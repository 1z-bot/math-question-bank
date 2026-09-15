"""Opt-in regressions against the real offline app, Chromium, and KaTeX.

Run with MATHBANK_TEST_BROWSER=1 python -m pytest --noconftest
tests/test_preview_browser.py. Requires the optional agent-browser CLI and its
browser; neither is an application dependency. A source-only temporary copy
gets its own database, uploads, token, server, and browser session.
"""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("MATHBANK_TEST_BROWSER") != "1",
    reason="opt-in actual browser regression",
)


class Browser:
    def __init__(self, executable, session):
        self.executable = executable
        self.session = session

    def command(self, *args, source=None):
        result = subprocess.run(
            [self.executable, "--session", self.session, "--json", *args],
            input=source, text=True, capture_output=True, timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload.get("success"), payload
        return payload.get("data")

    def evaluate(self, source):
        data = self.command("eval", "--stdin", source=source)
        return data.get("result") if isinstance(data, dict) and "result" in data else data

    def settle(self):
        self.evaluate("MathBankBrowserChecks.settle().then(() => true)")


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    executable = shutil.which("agent-browser")
    assert executable, "Install the optional agent-browser CLI to run this test"
    root = tmp_path_factory.mktemp("mathbank-browser")
    shutil.copy2(ROOT / "main.py", root / "main.py")
    for directory in ("mathbank", "templates", "static"):
        shutil.copytree(
            ROOT / directory, root / directory,
            ignore=shutil.ignore_patterns("__pycache__", "uploads", "test_uploads", "*.pyc"),
        )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, MATHBANK_LAUNCH_ID=uuid.uuid4().hex)
    env.pop("PYTHONPATH", None)
    browser = Browser(executable, "mathbank-test-" + uuid.uuid4().hex[:12])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (root / "server.log").open("w") as log:
        server = subprocess.Popen(
            [sys.executable, "-u", "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    with opener.open(url + "/healthz", timeout=1) as response:
                        if json.load(response).get("ready"):
                            break
                except (OSError, ValueError):
                    pass
                if server.poll() is not None:
                    pytest.fail((root / "server.log").read_text())
                time.sleep(0.1)
            else:
                pytest.fail((root / "server.log").read_text())
            browser.command("open", url)
            browser.command("set", "viewport", "1600", "1100")
            browser.command("wait", "--fn", "typeof window.renderPaperCanvas === 'function'")
            browser.evaluate("window.selectWorkspace('paper', '组卷排版工作台'); true")
            browser.command("wait", "--fn", "!!document.getElementById('a4PaperPreviewSheet') && !window.PaperStore.cartQuestionLoad.loading")
            browser.evaluate((ROOT / "tests" / "browser_preview_fixture.js").read_text())
            yield browser
        finally:
            try:
                subprocess.run(
                    [executable, "--session", browser.session, "close"],
                    capture_output=True, timeout=20, check=False,
                )
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def test_association_switching_and_real_unsaved_changes(browser, tmp_path):
    ids = browser.evaluate(r"""
    (async () => {
        selectWorkspace('bank', '题库研讨工作台');
        const ids = [];
        for (const content of ['关联回归甲：求 $1+2$。', '关联回归乙：求 $3+4$。']) {
            const form = new FormData();
            form.set('content', content);
            form.set('question_type', 'detailed_answer');
            form.set('difficulty', 'medium');
            const res = await fetch('/api/questions', {method: 'POST', body: form});
            const data = await res.json();
            if (!res.ok || !data.question) throw new Error(JSON.stringify(data));
            ids.push(data.question.id);
        }
        loadQuestions();
        return ids;
    })()
    """)
    first, second = ids

    def click_association_action(function_name):
        selector = f'button[onclick="{function_name}()"]'
        # The editor scrolls smoothly; settle before the coordinate-based click.
        browser.command("scrollintoview", selector)
        browser.settle()
        browser.command("click", selector)

    def open_question(question_id, related_id):
        browser.command("wait", "--fn", f"!!document.querySelector('#questionsList [data-id=\"{question_id}\"]')")
        browser.command("click", f'#questionsList [data-id="{question_id}"]')
        browser.command("wait", "--fn", (
            f"EditorState.questionId === {question_id} && !questionDetailLoading"
            f" && document.getElementById('editRelatedQuestion').value === '{related_id}'"
            " && !isEditorModified()"
        ))
        assert not browser.evaluate("!!document.getElementById('unsavedChangesModalTitle')")

    open_question(first, '')
    browser.command("wait", "--fn", f"!!document.querySelector('#editRelatedQuestion option[value=\"{second}\"]')")
    browser.command("select", "#editRelatedQuestion", str(second))
    assert browser.evaluate("document.getElementById('editRelatedQuestion').value") == str(second)
    click_association_action("associateRelatedQuestion")
    browser.settle()
    assert browser.evaluate(
        f"fetch('/api/questions/{first}/associated').then(r => r.json())"
        f".then(qs => qs.some(q => q.id === {second}))"
    ), browser.evaluate("({selection: document.getElementById('editRelatedQuestion').value, questionId: EditorState.questionId})")
    browser.command("wait", "--fn", "!isEditorModified() && !document.getElementById('paperAssociatedWrapper').classList.contains('hidden')")
    for question_id, related_id in [(second, first), (first, second), (second, first), (first, second)]:
        open_question(question_id, related_id)

    # Real DELETE persists and leaves the editor clean.
    click_association_action("clearRelatedQuestion")
    browser.command("wait", "--fn", "document.getElementById('editRelatedQuestion').value === '' && !isEditorModified()")
    assert browser.evaluate(f"fetch('/api/questions/{first}/associated').then(r => r.json())") == []

    # Linking must not mark a separately edited stem as saved.
    browser.command("fill", "#editContent", "真正尚未保存的题干修改")
    browser.command("select", "#editRelatedQuestion", str(second))
    click_association_action("associateRelatedQuestion")
    browser.command("wait", "--fn", "!document.getElementById('paperAssociatedWrapper').classList.contains('hidden')")
    assert browser.evaluate("isEditorModified()")
    browser.command("click", f'#questionsList [data-id="{second}"]')
    browser.command("wait", "#unsavedChangesModalTitle")
    assert browser.evaluate("EditorState.questionId") == first
    browser.command("screenshot", str(tmp_path / "association-real-unsaved-prompt.png"))
    # Close the modal without saving test edits, keeping other browser tests independent.
    browser.command("click", "#cancelBtn")
    browser.command("fill", "#editContent", "关联回归甲：求 $1+2$。")
    browser.evaluate("selectWorkspace('paper', '组卷排版工作台'); true")


def test_valid_and_naked_table_formulas_render_without_internal_placeholders(browser):
    result = browser.evaluate(r"""
    (() => {
        const samples = [
            String.raw`\begin{tabular}{cc}$x_1$ & $y_1$ \\ $x_2$ & $y_2$\end{tabular}`,
            String.raw`\begin{tabular}{cc}x_1 & $y_1$ \\ $\frac{1}{2}$ & y_2\end{tabular}`,
            String.raw`\begin{tabular}{cc}\multicolumn{2}{c}{$x_1+y_1$} \\ $x_2$ & y_2\end{tabular}`,
        ];
        return samples.map(source => {
            const host = document.createElement('div');
            window.renderQuestionPreviewContent(host, source);
            return {
                cells: host.querySelectorAll('td').length,
                math: host.querySelectorAll('.katex').length,
                errors: host.querySelectorAll('.katex-error').length,
                leaked: /PLACEHOLDER|@@|\uE000/.test(host.textContent),
                tex: [...host.querySelectorAll('annotation')].map(n => n.textContent),
            };
        });
    })()
    """)
    assert [row["cells"] for row in result] == [4, 4, 3], result
    assert [row["math"] for row in result] == [4, 4, 3], result
    assert all(not row["leaked"] and not row["errors"] for row in result), result
    assert result[0]["tex"] == ["x_1", "y_1", "x_2", "y_2"]


def test_resizing_repartitions_every_question_inside_footer_boundary(browser):
    browser.command("set", "viewport", "1600", "1100")
    browser.evaluate("MathBankBrowserChecks.seed(15, 2); true")
    browser.settle()
    wide = browser.evaluate("MathBankBrowserChecks.measure()")
    browser.command("set", "viewport", "1100", "900")
    browser.settle()
    narrow = browser.evaluate("MathBankBrowserChecks.measure()")
    for layout in (wide, narrow):
        assert [q["id"] for page in layout for q in page["questions"]] == list(range(1, 16))
        assert all(q["overflow"] <= 2 for page in layout for q in page["questions"]), layout
    assert len(narrow) > len(wide), (wide, narrow)


def test_oversize_page_stays_complete_after_repeated_title_updates(browser):
    browser.command("set", "viewport", "1600", "1100")
    browser.evaluate("MathBankBrowserChecks.seed(1, 60); true")
    browser.settle()
    layouts = [browser.evaluate("MathBankBrowserChecks.measure()")]
    for suffix in ("甲", "乙", "丙"):
        browser.command("fill", "#paperMetaTitle", "超长题检查" + suffix)
        browser.settle()
        layouts.append(browser.evaluate("MathBankBrowserChecks.measure()"))
    for layout in layouts:
        assert [q["id"] for page in layout for q in page["questions"]] == [1], layout
        assert any(page["expanded"] for page in layout), layout
        assert all(q["overflow"] <= 2 for page in layout for q in page["questions"]), layout


def test_header_typing_preserves_focus_and_second_character(browser):
    browser.evaluate("MathBankBrowserChecks.seed(4, 0, true); true")
    browser.settle()
    browser.command("click", ".canvas-meta-title")
    browser.command("press", "End")
    browser.command("press", "A")
    browser.settle()
    assert browser.evaluate("document.activeElement.matches('.canvas-meta-title')") is True
    browser.command("press", "B")
    browser.settle()
    assert browser.evaluate("document.querySelector('.canvas-meta-title').textContent") == "分页回归验证AB"


def test_dragging_uses_destination_question_order(browser):
    browser.evaluate("MathBankBrowserChecks.seed(4, 0, true); true")
    browser.settle()
    result = browser.evaluate("MathBankBrowserChecks.drag(1, 4)")
    assert result == [2, 3, 4, 1]
    browser.settle()
    result = browser.evaluate("MathBankBrowserChecks.drag(1, 2, true)")
    assert result == [1, 2, 3, 4]


def test_cross_page_drop_and_cancel_keep_the_cart_complete(browser):
    browser.evaluate("MathBankBrowserChecks.seed(15, 2); true")
    browser.settle()
    assert len(browser.evaluate("MathBankBrowserChecks.measure()")) > 1
    assert browser.evaluate("MathBankBrowserChecks.drag(1, 15, false, true)") == list(range(1, 16))
    browser.settle()
    assert browser.evaluate("MathBankBrowserChecks.drag(1, 15, false, false, true)") == list(range(2, 16)) + [1]
    browser.settle()
    layout = browser.evaluate("MathBankBrowserChecks.measure()")
    assert [q["id"] for page in layout for q in page["questions"]] == list(range(2, 16)) + [1]
    assert all(q["overflow"] <= 2 for page in layout for q in page["questions"]), layout


def test_returning_to_dragged_question_does_not_commit_old_hover_target(browser):
    browser.evaluate("MathBankBrowserChecks.seed(3, 0, true); true")
    browser.settle()
    assert browser.evaluate("MathBankBrowserChecks.drag(1, 3, false, false, false, true)") == [1, 2, 3]


@pytest.mark.parametrize("paper_type", ["exam", "quiz", "exam_19"])
def test_templates_preserve_mixed_question_types_and_solution_space(browser, paper_type):
    result = browser.evaluate(r"""
    (async () => {
        MathBankBrowserChecks.seed(19, 2);
        const state = window.PaperStore;
        state.meta.paper_type = %s;
        for (let id = 1; id <= 19; id++) {
            const q = state.questionsMap[id];
            if (id >= 9 && id <= 11) q.question_type = 'multi_choice';
            if (id >= 12 && id <= 14) {
                q.question_type = 'fill_in_blank';
                q.content = String.raw`已知 $x=1$，则 $x+1=$\fillin。`;
            }
            if (id >= 15) {
                q.question_type = 'detailed_answer';
                q.content = String.raw`已知函数 $f(x)=x^2$，求 $f(2)$ 并说明理由。`;
                state.cart[id - 1].solution_space = 3;
            }
        }
        window.renderPaperCanvas();
        await MathBankBrowserChecks.settle();
        return MathBankBrowserChecks.measure();
    })()
    """ % json.dumps(paper_type))
    assert [q["id"] for page in result for q in page["questions"]] == list(range(1, 20))
    assert all(q["overflow"] <= 2 for page in result for q in page["questions"]), result


def test_old_layout_callback_does_not_disconnect_current_observer(browser):
    result = browser.evaluate("""
    (() => {
        MathBankBrowserChecks.seed(15, 2);
        const oldCallback = window.scheduleActiveA4Repagination;
        window.renderPaperCanvas();
        const currentObserver = window.activeA4PaginationResizeObserver;
        oldCallback();
        return !!currentObserver && window.activeA4PaginationResizeObserver === currentObserver;
    })()
    """)
    assert result is True
