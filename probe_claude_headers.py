"""
Claude Code CLI 토큰으로 /v1/messages 를 한 번 호출해서
응답 헤더 전체를 파일로 저장하는 진단 스크립트.

- Cowork/Design 프로모션 크레딧 관련 필드가 헤더에 있는지 확인하기 위한 용도입니다.
- 토큰(Authorization) 값은 저장하지 않습니다.
- 실행 후 생성되는 claude_headers_dump.txt 내용을 그대로 공유해주시면
  ccusage_widget.py 에 파싱 로직을 추가하겠습니다.

사용법: python probe_claude_headers.py
"""
import json
import os
import urllib.request
import urllib.error
from pathlib import Path


def read_claude_token():
    p = Path.home() / ".claude" / ".credentials.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    # 흔한 두 가지 형태 모두 대응
    o = (data.get("claudeAiOauth") or data.get("oauth") or data)
    tok = o.get("accessToken") or o.get("access_token")
    if not tok:
        raise RuntimeError(f"토큰 필드를 못 찾음. 최상위 키: {list(data.keys())}")
    return tok


def main():
    tok = read_claude_token()
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "."}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST"
    )
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {tok}")
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("anthropic-beta", "oauth-2025-04-20")

    try:
        resp = urllib.request.urlopen(req, timeout=20)
        status, headers = resp.status, resp.headers
        resp.read()
    except urllib.error.HTTPError as e:
        status, headers = e.code, e.headers

    out_path = Path.home() / "claude_headers_dump.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"status={status}\n\n")
        for k, v in headers.items():
            if k.lower() == "authorization":
                continue
            f.write(f"{k}: {v}\n")

    print(f"완료. 저장 위치: {out_path}")
    print("파일 내용을 그대로 복사해서 공유해주세요 (토큰 값은 포함 안 됨).")


if __name__ == "__main__":
    main()
