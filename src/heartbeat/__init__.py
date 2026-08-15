"""claude-heartbeat 패키지.

버전의 단일 원천은 이 파일의 `__version__`이다. 빌드 메타데이터(pyproject)는
hatchling이 여기서 읽어 가고(`[tool.hatch.version]`), 런타임(`heartbeat --version`,
state.json `_daemon.version`)도 이 상수를 쓴다.

`importlib.metadata.version("claude-heartbeat")`를 런타임 원천으로 쓰지 않는 이유:
editable 설치의 메타데이터는 `pip install -e`를 마지막으로 돌린 시점에 고정된다.
그 뒤 git pull로 코드가 올라가도 메타데이터는 그대로라, 실측에서 실제로 어긋나
있었다(2026-08-05: 메타데이터 0.5.1 / pyproject 0.7.0 / 트리는 0.8.0 후보).
소비자 앱이 묻는 것은 "지금 도는 코드가 몇 버전인가"이므로, 코드와 같이 움직이는
상수가 맞는 답이다.
"""

__version__ = "0.9.6"

__all__ = ["__version__"]
