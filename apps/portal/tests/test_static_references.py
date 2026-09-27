"""Every file a template asks for exists.

`{% static %}` does not check anything in development: it builds a URL, the
browser asks for it, and a missing file is a 404 in a console nobody is reading.
That is how the port shipped three screens loading
`vendor/libs/flatpickr/flatpickr.js`, which was never copied across -- so every
date field in the project was a plain text box for as long as it took someone to
notice.

In production it is worse than quiet: WhiteNoise's manifest storage raises when
a referenced file is not in the manifest, so the same omission is a 500 on that
page. Tests do not catch it because conftest.py swaps in plain storage.

Hence this: read what the templates ask for, check it is on disk.
"""

import re
from pathlib import Path

from django.conf import settings

STATIC_TAG = re.compile(r"{%\s*static\s+['\"]([^'\"]+)['\"]")

# The template roots this project owns. Third-party packages are not our
# problem, and REST framework ships its own.
TEMPLATE_ROOTS = ["apps", "theme", "templates"]

# `{% static '/' %}` in master.html is Vuexy's data-assets-path: the static root
# itself, not a file.
IGNORED = {"/"}


def _static_dirs():
    dirs = [Path(d) for d in settings.STATICFILES_DIRS]
    return [d for d in dirs if d.exists()]


def _references():
    base = Path(settings.BASE_DIR)
    for root in TEMPLATE_ROOTS:
        for template in (base / root).rglob("*.html"):
            text = template.read_text(encoding="utf-8", errors="ignore")
            for path in STATIC_TAG.findall(text):
                if path in IGNORED or "{{" in path:
                    continue
                yield template.relative_to(base), path


def test_every_referenced_static_file_exists():
    references = sorted(set(_references()))
    assert len(references) > 20, "a regex that matches nothing would make this a no-op"
    dirs = _static_dirs()
    missing = [f"{template} loads {path!r}" for template, path in references
               if not any((d / path).exists() for d in dirs)]
    assert not missing, (
        "Not in static/ -- a silent 404 in development (the feature simply does not work) "
        "and a 500 in production (WhiteNoise's manifest storage raises):\n  " + "\n  ".join(missing)
    )
