import os
import glob
import re

for filepath in glob.glob("app/templates/*.html"):
    if filepath.endswith("base.html"):
        continue
    
    with open(filepath, "r") as f:
        content = f.read()

    if filepath.endswith("railway_account_billing_edit.html"):
        if "{% extends" not in content:
            new_content = "{% extends 'base.html' %}\n{% block content %}\n" + content + "\n{% endblock %}\n"
            with open(filepath, "w") as f:
                f.write(new_content)
        continue

    if filepath.endswith("dashboard.html"):
        match = re.search(r'<div class="main-content">(.*?)</div>\s*<div class="overlay"', content, re.DOTALL)
        if match:
            inner = match.group(1).strip()
            new_content = "{% extends 'base.html' %}\n{% block content %}\n<div class=\"main-content\">\n" + inner + "\n</div>\n{% endblock %}\n"
            with open(filepath, "w") as f:
                f.write(new_content)
        continue

    match = re.search(r'<main class="content">(.*?)</main>\s*</div>\s*</div>\s*<script>', content, re.DOTALL)
    if match:
        inner = match.group(1).strip()
        new_content = "{% extends 'base.html' %}\n{% block content %}\n<div class=\"main-content\">\n" + inner + "\n</div>\n{% endblock %}\n"
        with open(filepath, "w") as f:
            f.write(new_content)
    else:
        match2 = re.search(r'<div class="login-body">(.*?)</div>\s*</body>', content, re.DOTALL)
        if match2:
            inner = '<div class="login-body">\n' + match2.group(1).strip() + '\n</div>'
            new_content = "{% extends 'base.html' %}\n{% block content %}\n" + inner + "\n{% endblock %}\n"
            with open(filepath, "w") as f:
                f.write(new_content)

print("Done stripping shells.")
