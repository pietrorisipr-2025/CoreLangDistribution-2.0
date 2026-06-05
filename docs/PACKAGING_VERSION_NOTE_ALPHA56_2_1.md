# CLD2 alpha56.2.1 - packaging version note

Alpha56.2 introduced local installation with:

```bash
python -m pip install -e .
```

That exposed a packaging bug: `pyproject.toml` used the historical CLD2 string `2.0.0-alpha50.2`, which is not valid PEP 440 syntax for Python packaging.

Alpha56.2.1 fixes this by using:

```toml
version = "2.0.0a56.post2"
```

This is the Python package version. It is intentionally separate from the implementation-baseline note:

```text
core implementation baseline: 2.0.0-alpha50.2
```

The core transfer model did not change in this hotfix.

Expected install path:

```bash
python -m pip install -e .
cld2 --version
```

Expected CLI version:

```text
2.0.0a56.post2
```
