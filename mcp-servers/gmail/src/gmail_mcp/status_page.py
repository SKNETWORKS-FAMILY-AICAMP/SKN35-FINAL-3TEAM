"""서버 주소(/)를 브라우저로 열었을 때 보여줄 안내 페이지.

MCP 서버는 웹 앱이 아니라 /mcp 로 AI가 통신하는 서버다. 루트 주소로 들어온 사람에게
"서버는 정상이고, 툴은 이렇게 써 보면 된다"를 알려준다.
"""

from __future__ import annotations

from html import escape

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} MCP 서버</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--text:#1f2328;--muted:#59636e;--border:#d8dee4;--ok:#1a7f37;--warn:#b35900;--code:#eff1f3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#161a20;--text:#e6e8eb;--muted:#9aa4af;--border:#2c333b;--ok:#56d364;--warn:#f0a35e;--code:#222831}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 "Malgun Gothic",system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:32px 16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px 24px;margin:16px 0}}
h1{{font-size:1.5rem;margin:0 0 4px}} h2{{font-size:1.05rem;margin:0 0 10px}}
.ok{{color:var(--ok);font-weight:600}} .warn{{color:var(--warn);font-weight:600}} .muted{{color:var(--muted)}}
code{{background:var(--code);padding:1px 6px;border-radius:4px;font-family:Consolas,monospace;font-size:.9em}}
table{{border-collapse:collapse;width:100%}} td{{padding:6px 4px;border-top:1px solid var(--border);vertical-align:top}}
ol{{padding-left:20px;margin:0}} li{{margin:4px 0}}
</style></head><body><main>
<h1>{name} MCP 서버</h1>
<div class="muted">이 주소는 웹 화면이 아니라 AI 에이전트가 툴을 호출하는 서버입니다.</div>

<div class="card">
<h2>상태</h2>
<table>
<tr><td>서버</td><td class="ok">● 정상 실행 중</td></tr>
<tr><td>모드</td><td class="{mode_class}">{mode}</td></tr>
<tr><td>MCP 주소</td><td><code>{mcp_url}</code></td></tr>
<tr><td>상태 확인</td><td><a href="/health"><code>/health</code></a></td></tr>
</table>
</div>

<div class="card">
<h2>툴 ({tool_count}개)</h2>
<table>{tool_rows}</table>
</div>

<div class="card">
<h2>브라우저에서 툴을 실행해 보려면 (MCP Inspector)</h2>
<ol>
<li>새 터미널에서 <code>npx @modelcontextprotocol/inspector</code> 실행</li>
<li>열린 Inspector 화면에서 <b>Add Servers → Add manually</b></li>
<li>Transport <b>streamable-http</b>, URL <code>{mcp_url}</code> 입력 후 Add</li>
<li>카드의 토글을 켜서 연결 → 위쪽 <b>Tools</b> 탭에서 툴 선택 → <b>Execute Tool</b></li>
</ol>
</div>
</main></body></html>"""


def render_status_page(*, name: str, mode: str, simulated: bool, mcp_url: str, tools: list[tuple[str, str]]) -> str:
    rows = "".join(f"<tr><td><code>{escape(n)}</code></td><td>{escape(d)}</td></tr>" for n, d in tools)
    return PAGE.format(
        name=escape(name),
        mode=escape(mode),
        mode_class="warn" if simulated else "ok",
        mcp_url=escape(mcp_url),
        tool_count=len(tools),
        tool_rows=rows,
    )
