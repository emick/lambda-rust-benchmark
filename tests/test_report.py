import json
from pathlib import Path
from benchmark.report import render


def test_static_report_preserves_incomplete_data_and_safe_html(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name,value in [("samples.json",[]), ("metadata.json",{"region":"eu-north-1", "started_at":"2026-10-11",
                       "notes":["</script><script>bad()</script>"], "cells":[]}), ("initializations.json",[])]:
        (source / name).write_text(json.dumps(value))
    output=tmp_path / "public"
    render(source,output)
    content=(output / "index.html").read_text()
    assert "{{" not in content
    assert "</script><script>bad()" not in content
    data=json.loads((output / "summary.json").read_text())
    assert not data["audit"]["complete"]
    assert len(data["audit"]["incomplete"])==96
    assert (output / "samples.jsonl.gz").is_file()
    assert (output / ".nojekyll").is_file()
