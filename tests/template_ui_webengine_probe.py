"""Check exclusion wrappers against the installed bundle in Anki's Qt WebEngine."""

import argparse
import json

from gui_safety import gui_arguments

gui_arguments(argparse.ArgumentParser(description=__doc__), allow_offscreen=True)

import support
from shirabe_addon import assets, template_ui
from aqt.qt import QApplication, QEventLoop, QTimer, QWebEngineView


app = QApplication(['shirabe-template-exclusion-probe'])
view = QWebEngineView()
view.resize(640, 480)
view.show()
loop = QEventLoop()
result = {}
source = '<div>{{Expression}}</div><section>{{Notes}}</section>'
excluded = template_ui.with_excluded_fields(source, ('Notes',))
rendered = excluded.replace('{{Expression}}', '<ruby>猫<rt>ねこ</rt></ruby>です。').replace(
    '{{Notes}}', '<div>秘密</div><b>非公開</b>')
bundle = (assets.WEB / 'shirabe-card.js').read_text()


def captured(value):
    result.update(value or {})
    loop.quit()


def loaded(ok):
    if not ok:
        result['error'] = 'load_failed'
        loop.quit()
        return
    view.page().runJavaScript('''(() => {
      const root = document.getElementById('qa');
      const wrapper = root.querySelector('[data-shirabe-ignore]');
      return {
        runs: ShirabeCard.collectReadableRuns(root),
        notes: wrapper.textContent,
        markup: wrapper.innerHTML,
        display: getComputedStyle(wrapper).display
      };
    })()''', captured)


view.loadFinished.connect(loaded)
view.setHtml('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'">'
             '<script>' + bundle.replace('</script', '<\\/script') + '</script><main id="qa">' + rendered + '</main>')
QTimer.singleShot(10000, loop.quit)
loop.exec()
view.close()
assert ''.join(result.get('runs', ())) == '猫です。', result
assert result.get('notes') == '秘密非公開', result
assert result.get('markup') == '<div>秘密</div><b>非公開</b>', result
assert result.get('display') == 'contents', result
print(json.dumps(result, ensure_ascii=False))
