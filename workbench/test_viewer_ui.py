"""Viewer control contracts and display-only UI behavior."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from preview_package import template

ROOT = Path(__file__).parent

class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == 'id': self.ids.append(value)

class ViewerUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node unavailable')
    def test_filtered_batch_preserves_review_gate_and_keyboard_navigation(self):
        reply = subprocess.run(['node', str(ROOT/'qa/test_viewer_ui.cjs')], capture_output=True, text=True, check=True)
        self.assertTrue(all(json.loads(reply.stdout).values()))

    def test_standalone_template_has_unique_controls_and_embedded_ui(self):
        page = template()
        parser = Elements(); parser.feed(page)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertNotIn('__UI__', page)
        self.assertIn('window.ViewerUI=', page)
        for control in ['interaction-quality', 'rotation-momentum', 'interaction-context', 'ct-render-mode', 'target', 'confirm', 'boundaries', 'layer-search']:
            self.assertIn(control, parser.ids)
        css = (ROOT/'assets/viewer.css').read_text(encoding='utf-8')
        self.assertEqual(set(re.findall(r'font-size:(\d+)px', css)), {'12','14','18'})
        self.assertNotRegex(css, r'border(?:-(?:top|right|bottom|left|width))?:\s*(?:1|2)px')
        self.assertNotIn('box-shadow:', css)
        self.assertNotIn('transition:', css)

if __name__ == '__main__': unittest.main()
