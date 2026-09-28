"""로컬 실행: 실제 AWS 없이 가짜 AWS(moto) 위에서 Vigie의 MCP 서버를 띄우고 장애 시나리오를 심는다.

- world.py: 정상 환경과 장애 시나리오 (진단 채점 테스트 tests/test_diagnose.py와 같은 코드)
- stack.py: 가짜 AWS + 로컬 Streamable HTTP MCP 서버 + 시나리오 관리 주소를 한 프로세스에 띄운다
- scenario.py: 떠 있는 stack에 시나리오를 심고 되돌리는 명령
"""
