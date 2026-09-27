"""AWS 공식 네트워크 MCP 서버 (mcp/lambda_mcp/official.py): VPC·ENI·경로 추적만, 읽기 전용

- EC2가 놓인 네트워크(VPC·서브넷·보안 그룹·NACL·라우팅·ENI·흐름 로그)와 경로 추적 도구 6개만 붙인다.
  Cloud WAN·Transit Gateway·Network Firewall·VPN 도구 21개는 이 계정에 없는 서비스라 뺀다
- profile_name(다른 계정의 프로필)은 Lambda에서 쓸 수 없어 스키마에서 숨기고 비워 둔다
- 이 서버는 import할 때 프로세스 전체의 기본 로거를 DEBUG로 바꾼다 (MCP SDK의 MCPServer도 INFO로 바꾼다).
  공식 서버를 모두 불러온 뒤 되돌린다
조회는 moto로 실제 EC2 API까지 부른다.
"""
import json
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
import yaml

from conftest import ROOT
from test_approvals import env  # noqa: F401 (fixture)

INCLUDED = {"get_path_trace_methodology", "find_ip_address", "get_eni_details", "list_vpcs", "get_vpc_network",
            "get_vpc_flow_logs"}
EXCLUDED_SAMPLE = {"list_transit_gateways", "get_tgw", "list_core_networks", "simulate_cwan_route_change",
                   "list_firewalls", "get_firewall_flow_logs", "list_vpn_connections"}


@pytest.fixture
def net_env(env):  # noqa: F811
    tools = {t["name"]: t for t in json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}
    ec2 = boto3.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName="vigie-web", Description="web", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
    eni = ec2.create_network_interface(SubnetId=subnet, Groups=[sg], PrivateIpAddress="10.0.1.10")
    return env, tools, {"vpc": vpc, "subnet": subnet, "sg": sg, "eni": eni["NetworkInterface"]["NetworkInterfaceId"]}


def text_of(result):
    assert not result.get("isError"), result
    return json.dumps(result, ensure_ascii=False)


def test_only_vpc_and_path_tools_are_attached(net_env):
    _, tools, _ = net_env
    assert INCLUDED <= set(tools) and not (EXCLUDED_SAMPLE & set(tools))
    assert all(tools[name]["_meta"]["vigie/risk"] == "read" for name in INCLUDED)
    # 다른 계정의 프로필 이름은 모델에게 보이지 않는다
    assert not any("profile_name" in tools[name]["inputSchema"].get("properties", {}) for name in INCLUDED)
    # region은 필수가 아니다: 생략하면 이 배포의 리전을 채워 넣는다 (list_vpcs는 원래 기본값 없는 필수 인자)
    assert not any("region" in tools[name]["inputSchema"].get("required", []) for name in INCLUDED)


def test_vpc_and_eni_details_are_read(net_env):
    env, _, ids = net_env
    assert ids["vpc"] in text_of(env["mcp"].call_admin("list_vpcs", {}))
    eni = text_of(env["mcp"].call_admin("get_eni_details", {"eni_id": ids["eni"]}))
    assert ids["sg"] in eni and "443" in eni
    assert ids["subnet"] in text_of(env["mcp"].call_admin("get_vpc_network", {"vpc_id": ids["vpc"]}))
    assert text_of(env["mcp"].call_admin("get_path_trace_methodology", {}))


def test_profile_name_from_the_model_is_ignored(net_env):
    # 모델이 profile_name을 보내도 비워 둔 값이 이긴다 (Lambda 역할로 조회)
    env, _, ids = net_env
    assert ids["vpc"] in text_of(env["mcp"].call_admin("list_vpcs", {"profile_name": "other-account"}))


ROOT_LOGGER_CHECK = r"""
import json, logging, sys
sys.path.insert(0, sys.argv[1])  # mcp 폴더
from moto import mock_aws
root = logging.getLogger()
before = (root.level, len(root.handlers))
with mock_aws():
    from lambda_mcp.official import OfficialTools
    names = [s["name"] for s in OfficialTools.default().schemas()]
import awslabs.aws_network_mcp_server.server  # 정말 불러왔는지
print(json.dumps({"before": before, "after": (root.level, len(root.handlers)), "has_network": "list_vpcs" in names}))
"""


def test_loading_official_servers_does_not_change_the_root_logger():
    # 이 프로세스는 이미 공식 서버를 불러왔으므로 새 프로세스에서 확인한다
    done = subprocess.run([sys.executable, "-c", ROOT_LOGGER_CHECK, str(Path(ROOT) / "mcp")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["has_network"] and result["after"] == result["before"]


def test_mcp_role_gets_only_ec2_describe():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=Loader)
    statements = template["Resources"]["McpLambdaExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"]
    # EC2 권한은 조회뿐이다. 변경은 승인을 거쳐 실행되는 중지·시작 두 가지만 있다 (PR 13)
    actions = {a for s in statements if s["Effect"] == "Allow" for a in s["Action"] if a.startswith("ec2:")}
    changes = {a for a in actions if not a.startswith("ec2:Describe")}
    assert actions and changes == {"ec2:StopInstances", "ec2:StartInstances"}, changes
