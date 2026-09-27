from .mcp_types import (
    JSONRPCRequest, 
    JSONRPCResponse,
    JSONRPCError,
    InitializeResult,
    ServerInfo,
    Capabilities,
    TextContent,
    ErrorContent
)
from .session import SessionManager
from .approval import ACTION_ID_META, PREVIEW_META
from .risk import annotate, is_allowed, needs_approval
import json
import logging
from typing import Optional, Any, Dict, Callable, get_type_hints, List, TypeVar, Generic, Union, get_args, get_origin
import inspect
import functools
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# Context variable to store current session ID
current_session_id: ContextVar[Optional[str]] = ContextVar('current_session_id', default=None)

T = TypeVar('T')

class SessionData(Generic[T]):
    """Helper class for type-safe session data access"""
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def get(self, key: str, default: T = None) -> T:
        """Get a value from session data with type safety"""
        return self._data.get(key, default)

    def set(self, key: str, value: T) -> None:
        """Set a value in session data"""
        self._data[key] = value

    def raw(self) -> Dict[str, Any]:
        """Get the raw dictionary data"""
        return self._data

class LambdaMCPServer:
    """A class to handle MCP protocol in AWS Lambda"""
    
    def __init__(self, name: str, version: str = "1.0.0", session_table: str = "mcp_sessions"):
        self.name = name
        self.version = version
        self.tools: Dict[str, Dict] = {}
        self.tool_implementations: Dict[str, Callable] = {}
        # 다른 곳에서 가져온 도구 (AWS 공식 MCP 서버, official.OfficialTools). 없으면 직접 만든 도구만 쓴다
        self.external = None
        # 변경 도구의 미리 보기 (도구 이름 → 함수). 실행하지 않고 지금 상태와 바뀔 내용을 돌려준다
        self.previews: Dict[str, Callable] = {}
        # 변경 도구 실행 직전의 승인 재확인 (approval.ApprovalGate). 없으면 변경 도구는 하나도 실행되지 않는다
        self.approval_gate = None
        self.session_manager = SessionManager(table_name=session_table)
        # Ensure session table exists
        self.session_manager.create_table(table_name=session_table)
    
    def attach(self, external) -> None:
        """다른 곳에서 가져온 도구 묶음을 붙인다. schemas() · has(name) · call(name, args)를 갖춰야 한다.

        tools/list에는 직접 만든 도구와 함께 보이고, tools/call은 이름으로 해당 묶음에 넘긴다.
        """
        self.external = external

    def preview(self, tool_name: str):
        """변경 도구의 미리 보기 함수를 등록한다. 도구와 같은 인자를 받아 {summary, before, after}를 돌려준다.
        조회 권한만으로 동작해야 한다 (승인 전에 부른다)."""
        def decorator(func: Callable):
            self.previews[tool_name] = func
            return func
        return decorator

    def get_session(self) -> Optional[SessionData]:
        """Get the current session data wrapper.
        
        Returns:
            SessionData object or None if no session exists
        """
        session_id = current_session_id.get()
        if not session_id:
            return None
        data = self.session_manager.get_session(session_id)
        return SessionData(data) if data is not None else None

    def set_session(self, data: Dict[str, Any]) -> bool:
        """Set the entire session data.
        
        Args:
            data: New session data
            
        Returns:
            True if successful, False if no session exists
        """
        session_id = current_session_id.get()
        if not session_id:
            return False
        return self.session_manager.update_session(session_id, data)

    def update_session(self, updater_func: Callable[[SessionData], None]) -> bool:
        """Update session data using a function.
        
        Args:
            updater_func: Function that takes SessionData and updates it in place
            
        Returns:
            True if successful, False if no session exists
        """
        session = self.get_session()
        if not session:
            return False
            
        # Update the session data
        updater_func(session)
        
        # Save back to storage
        return self.set_session(session.raw())

    def tool(self):
        """Decorator to register a function as an MCP tool.
        
        Uses function name, docstring, and type hints to generate the MCP tool schema.
        """
        def decorator(func: Callable):
            # Get function name and convert to camelCase for tool name
            func_name = func.__name__
            tool_name = ''.join([func_name.split('_')[0]] + [word.capitalize() for word in func_name.split('_')[1:]])
            
            # Get docstring and parse into description
            doc = inspect.getdoc(func) or ""
            description = doc.split('\n\n')[0]  # First paragraph is description
            
            # Get type hints
            hints = get_type_hints(func)
            # 기본값이 있는 인자는 필수가 아니다 (예: 버킷을 주지 않으면 모든 버킷을 점검)
            parameters = inspect.signature(func).parameters
            return_type = hints.pop('return', Any)
            
            # Build input schema from type hints and docstring
            properties = {}
            required = []
            
            # Parse docstring for argument descriptions
            arg_descriptions = {}
            if doc:
                lines = doc.split('\n')
                in_args = False
                for line in lines:
                    if line.strip().startswith('Args:'):
                        in_args = True
                        continue
                    if in_args:
                        if not line.strip() or line.strip().startswith('Returns:'):
                            break
                        if ':' in line:
                            arg_name, arg_desc = line.split(':', 1)
                            arg_descriptions[arg_name.strip()] = arg_desc.strip()

            # Build properties from type hints
            for param_name, param_type in hints.items():
                # Optional[int] 등은 안쪽 타입으로 본다
                if get_origin(param_type) is Union:
                    inner = [arg for arg in get_args(param_type) if arg is not type(None)]
                    param_type = inner[0] if inner else str
                param_schema = {"type": "string"}  # Default to string
                if param_type == int:
                    param_schema["type"] = "integer"
                elif param_type == float:
                    param_schema["type"] = "number"
                elif param_type == bool:
                    param_schema["type"] = "boolean"
                
                if param_name in arg_descriptions:
                    param_schema["description"] = arg_descriptions[param_name]
                    
                properties[param_name] = param_schema
                default = parameters[param_name].default if param_name in parameters else inspect.Parameter.empty
                if default is inspect.Parameter.empty:
                    required.append(param_name)
                elif default is not None:
                    param_schema["default"] = default
            
            # Create tool schema
            tool_schema = {
                "name": tool_name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
            
            # Register the tool
            self.tools[tool_name] = tool_schema
            self.tool_implementations[tool_name] = func
            
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            
            return wrapper
        
        return decorator

    def _create_error_response(self, code: int, message: str, request_id: Optional[str] = None, error_content: Optional[List[Dict]] = None, session_id: Optional[str] = None, status_code: Optional[int] = None) -> Dict:
        """Create a standardized error response"""
        error = JSONRPCError(code=code, message=message)
        response = JSONRPCResponse(jsonrpc="2.0", id=request_id, error=error, errorContent=error_content)
        
        headers = {
            "Content-Type": "application/json",
            "MCP-Version": "0.6"
        }
        if session_id:
            headers["MCP-Session-Id"] = session_id
            
        return {
            "statusCode": status_code or self._error_code_to_http_status(code),
            "body": response.model_dump_json(),
            "headers": headers
        }
    
    def _error_code_to_http_status(self, error_code: int) -> int:
        """Map JSON-RPC error codes to HTTP status codes"""
        error_map = {
            -32700: 400,  # Parse error
            -32600: 400,  # Invalid Request
            -32601: 404,  # Method not found
            -32602: 400,  # Invalid params
            -32603: 500,  # Internal error
        }
        return error_map.get(error_code, 500)
    
    def _create_success_response(self, result: Any, request_id: str, session_id: Optional[str] = None) -> Dict:
        """Create a standardized success response"""
        response = JSONRPCResponse(jsonrpc="2.0", id=request_id, result=result)
        
        headers = {
            "Content-Type": "application/json",
            "MCP-Version": "0.6"
        }
        if session_id:
            headers["MCP-Session-Id"] = session_id
            
        return {
            "statusCode": 200,
            "body": response.model_dump_json(),
            "headers": headers
        }

    def _call_write_tool(self, tool_name: str, tool_args: Dict, action_id: Optional[str], request_id,
                         session_id) -> Dict:
        """승인된 작업이면 한 번만 실행하고 결과를 승인 테이블에 남긴다. 아니면 실행하지 않고 이유를 돌려준다."""
        def refused(reason: str) -> Dict:
            logger.warning("변경 도구 %s 실행 거절: %s", tool_name, reason)
            content = [TextContent(text=f"실행하지 않았습니다: {reason}").model_dump()]
            return self._create_success_response({"content": content, "isError": True}, request_id, session_id)

        if tool_name not in self.tools:
            return refused("변경 도구 목록에 없는 도구입니다.")
        if self.approval_gate is None:
            return refused("승인 확인 설정이 없어 변경 도구를 실행할 수 없습니다.")
        ok, reason = self.approval_gate.claim(action_id, tool_name, tool_args)
        if not ok:
            return refused(reason)
        try:
            result = self.tool_implementations[tool_name](**tool_args)
        except Exception as e:
            logger.error(f"Error executing write tool {tool_name}: {e}")
            self.approval_gate.finish(action_id, False, str(e))
            content = [TextContent(text=str(e)).model_dump()]
            return self._create_success_response({"content": content, "isError": True}, request_id, session_id)
        text = json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, (dict, list)) else str(result)
        self.approval_gate.finish(action_id, True, text)
        return self._create_success_response({"content": [TextContent(text=text).model_dump()]}, request_id,
                                             session_id)

    def handle_request(self, event: Dict, context: Any) -> Dict:
        """Handle an incoming Lambda request"""
        request_id = None
        session_id = None
        
        try:
            # Log the full event for debugging
            logger.debug(f"Received event: {event}")
            
            # Get headers (case-insensitive)
            headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
            
            # Get session ID from headers
            session_id = headers.get("mcp-session-id")
            
            # Set current session ID in context
            if session_id:
                current_session_id.set(session_id)
            else:
                current_session_id.set(None)
            
            # Check HTTP method for session deletion
            if event.get("httpMethod") == "DELETE":
                if not session_id:
                    return {"statusCode": 400, "body": "Missing session ID"}
                    
                if self.session_manager.delete_session(session_id):
                    return {"statusCode": 204}
                else:
                    return {"statusCode": 404}
            
            # Validate content type
            if headers.get("content-type") != "application/json":
                return self._create_error_response(-32700, "Unsupported Media Type")

            try:
                body = json.loads(event["body"])
                logger.debug(f"Parsed request body: {body}")
                request_id = body.get("id") if isinstance(body, dict) else None
                
                # Check if this is a notification (no id field)
                if isinstance(body, dict) and "id" not in body:
                    logger.debug("Request is a notification")
                    return {
                        "statusCode": 204,
                        "body": "",
                        "headers": {"Content-Type": "application/json", "MCP-Version": "0.6"}
                    }
                    
                # Validate basic JSON-RPC structure
                if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
                    return self._create_error_response(-32700, "Parse error", request_id)
                    
            except json.JSONDecodeError:
                return self._create_error_response(-32700, "Parse error")
            
            # Parse and validate the request
            request = JSONRPCRequest.model_validate(body)
            logger.debug(f"Validated request: {request}")
            
            # Handle initialization request
            if request.method == "initialize":
                logger.info("Handling initialize request")
                # Create new session
                session_id = self.session_manager.create_session()
                current_session_id.set(session_id)
                result = InitializeResult(
                    protocolVersion="2024-11-05",
                    serverInfo=ServerInfo(name=self.name, version=self.version),
                    capabilities=Capabilities(tools={"list": True, "call": True})
                )
                return self._create_success_response(result.model_dump(), request.id, session_id)
            
            # For all other requests, validate session if provided
            if session_id:
                session_data = self.session_manager.get_session(session_id)
                if session_data is None:
                    return self._create_error_response(-32000, "Invalid or expired session", request.id, status_code=404)
            elif request.method != "initialize":
                return self._create_error_response(-32000, "Session required", request.id, status_code=400)
                
            # Handle tools/list request
            if request.method == "tools/list":
                logger.info("Handling tools/list request")
                tools = list(self.tools.values())
                if self.external:
                    tools += self.external.schemas()
                # 도구마다 위험도(조회·결과물·변경)를 붙인다. LLM Lambda는 이것으로 승인이 필요한지 정한다 (risk.py)
                return self._create_success_response({"tools": [annotate(tool) for tool in tools]},
                                                     request.id, session_id)
            
            # Handle tool calls
            if request.method == "tools/call":
                tool_name = request.params.get("name")
                tool_args = request.params.get("arguments", {})
                meta = request.params.get("_meta") or {}

                # 변경 도구의 미리 보기: 실행하지 않고 지금 상태와 바뀔 내용만 돌려준다 (승인 요청 카드에 쓴다)
                if meta.get(PREVIEW_META):
                    if tool_name not in self.previews:
                        return self._create_error_response(-32602, f"Tool '{tool_name}' has no preview",
                                                           request.id, session_id=session_id)
                    try:
                        preview = self.previews[tool_name](**tool_args)
                        content = [TextContent(text=json.dumps(preview, ensure_ascii=False)).model_dump()]
                        return self._create_success_response({"content": content}, request.id, session_id)
                    except Exception as e:  # 잘못된 인자(범위 밖 리소스 등)는 모델이 읽도록 결과로 돌려준다
                        content = [TextContent(text=str(e)).model_dump()]
                        return self._create_success_response({"content": content, "isError": True},
                                                             request.id, session_id)

                if tool_name not in self.tools and not (self.external and self.external.has(tool_name)):
                    return self._create_error_response(-32601, f"Tool '{tool_name}' not found", request.id,
                                                       session_id=session_id)

                # 관리자 전용 도구(CloudTrail·IAM·네트워크 등, risk.ADMIN_ONLY): LLM Lambda가 요청자의 그룹을 확인하고
                # 관리자라고 붙여 보낸 호출만 실행한다. LLM Lambda도 막지만, 그쪽 코드에 실수가 있어도 여기서 한 번 더 막는다.
                # 모델이 읽고 사용자에게 설명하도록 JSON-RPC 오류가 아니라 isError 결과로 돌려준다
                if not is_allowed(tool_name, meta):
                    content = [TextContent(text=f"'{tool_name}'은(는) 관리자(admins 그룹)만 쓸 수 있는 도구라 "
                                                "실행하지 않았습니다.").model_dump()]
                    return self._create_success_response({"content": content, "isError": True},
                                                         request.id, session_id)

                # 변경 도구(위험도 목록에 없는 공식 도구 포함): 승인 테이블을 직접 다시 확인한 뒤에만 실행한다 (approval.py)
                if needs_approval(tool_name):
                    return self._call_write_tool(tool_name, tool_args, meta.get(ACTION_ID_META),
                                                 request.id, session_id)
                
                # 공식 MCP 서버 도구: 결과(content)와 오류 여부(isError)를 MCP 형식 그대로 돌려준다.
                # 도구 안의 오류(권한 없음 등)는 JSON-RPC 오류가 아니라 isError 결과다 (MCP 규약). 모델이 읽고 다음 수를 정한다
                if tool_name not in self.tools and self.external and self.external.has(tool_name):
                    try:
                        content, is_error = self.external.call(tool_name, tool_args)
                        return self._create_success_response({"content": content, "isError": is_error}, request.id, session_id)
                    except Exception as e:
                        logger.error(f"Error executing official tool {tool_name}: {e}")
                        error_content = [ErrorContent(text=str(e)).model_dump()]
                        return self._create_error_response(-32603, f"Error executing tool: {str(e)}", request.id, error_content, session_id)
                if tool_name not in self.tools:
                    return self._create_error_response(-32601, f"Tool '{tool_name}' not found", request.id, session_id=session_id)
                
                try:
                    result = self.tool_implementations[tool_name](**tool_args)
                    # 사전·목록은 JSON으로 보낸다 (Python 표현 str(dict)보다 모델이 읽기 쉽고, 날짜 등도 글자로 바뀐다)
                    text = (json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, (dict, list))
                            else str(result))
                    content = [TextContent(text=text).model_dump()]
                    return self._create_success_response({"content": content}, request.id, session_id)
                except Exception as e:
                    logger.error(f"Error executing tool {tool_name}: {e}")
                    error_content = [ErrorContent(text=str(e)).model_dump()]
                    return self._create_error_response(-32603, f"Error executing tool: {str(e)}", request.id, error_content, session_id)

            # Handle unknown methods
            return self._create_error_response(-32601, f"Method not found: {request.method}", request.id, session_id=session_id)

        except Exception as e:
            logger.error(f"Error processing request: {str(e)}", exc_info=True)
            return self._create_error_response(-32000, str(e), request_id, session_id=session_id)
        finally:
            # Clear session context
            current_session_id.set(None) 