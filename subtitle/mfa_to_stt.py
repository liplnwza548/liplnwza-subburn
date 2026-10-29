# Vendored from the 2026-10-29 sync experiment (proven on clip 09, 16-line script).
# Colab use: python subtitle/mfa_to_stt.py <approved_script.txt> <mfa_out.json> <NN_mfa.json>
# Requires: pythainlp (in subtitle/viralpop/requirements.txt). Do not edit the logic without re-running the clip-09 check.
"""Convert MFA align_one_hf JSON output -> burner STT json.

Maps MFA words positionally onto the burner's tokenization (per-line newmm
+ bare repetition mark attached). MFA <unk> entries (OOV, e.g. 'แน่นๆ') take
the script token at the same position. Asserts counts match.
"""
import json
import sys
from pathlib import Path

from pythainlp import word_tokenize

script_path, mfa_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
script_text = Path(script_path).read_text(encoding="utf-8")
toks = []
for ln in script_text.split("\n"):
    ln = ln.strip()
    if ln:
        toks.extend(t for t in word_tokenize(ln, engine="newmm", keep_whitespace=False) if t.strip())
fx = []
for t in toks:
    if t == "ๆ" and fx:
        fx[-1] += t
    else:
        fx.append(t)

d = json.loads(Path(mfa_path).read_text(encoding="utf-8"))
ents = [e for e in d["tiers"]["words"]["entries"] if e[2] not in ("<eps>", "")]
assert len(ents) == len(fx), f"count mismatch: mfa={len(ents)} script={len(fx)}"
words = []
for (s, e, w), tok in zip(ents, fx):
    assert w == tok or w == "<unk>", (w, tok)
    words.append({"word": tok, "start": round(s, 3), "end": round(e, 3)})
Path(out_path).write_text(json.dumps({"words": words}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"mfa words: {len(words)}")
for w in words:
    print(f"{w['start']:6.2f}-{w['end']:6.2f}  {w['word']}")
