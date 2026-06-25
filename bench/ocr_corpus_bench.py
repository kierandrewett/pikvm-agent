"""OCR corpus benchmark — pull real desktop screenshots (Wikimedia Commons,
normalised to 1280px like the KVM stream) and compare PaddleOCR against full-frame
tesseract. Not part of the default suite (needs the network + the [vision] extra).
Run:  .venv/bin/python bench/ocr_corpus_bench.py
"""
import subprocess, re, io, time, statistics as st
from pathlib import Path
import httpx
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

CORPUS=Path("/home/kieran/dev/pikvm-agent/bench/screens"); CORPUS.mkdir(parents=True,exist_ok=True)
UA={"User-Agent":"pikvm-agent-ocr-bench/1.0 (kieran research; contact local)"}
cli=httpx.Client(follow_redirects=True, headers=UA, timeout=60)

def fetch_urls(n=14):
    params={"action":"query","format":"json","generator":"search",
        "gsrsearch":"screenshot software application desktop window","gsrnamespace":"6",
        "gsrlimit":str(n*3),"prop":"imageinfo","iiprop":"url|mime","iiurlwidth":"1280"}
    r=cli.get("https://commons.wikimedia.org/w/api.php",params=params)
    out=[]
    for p in r.json().get("query",{}).get("pages",{}).values():
        ii=p.get("imageinfo")
        if ii and ii[0].get("mime") in ("image/png","image/jpeg"):
            u=ii[0].get("thumburl") or ii[0].get("url")
            if u: out.append((re.sub(r'^File:','',p.get("title","?")),u))
    return out[:n]

def wordy(s):
    toks=re.findall(r"[A-Za-z]{2,}",s); good=[t for t in toks if re.search(r"[aeiou]",t.lower())]
    return len(toks),(len(good)/len(toks) if toks else 0.0)

def dl(u):
    for _ in range(3):
        try:
            b=cli.get(u).content
            if b and len(b)>2000: return Image.open(io.BytesIO(b)).convert("RGB")
        except Exception: pass
        time.sleep(1.2)
    return None

print("Building corpus (throttled)…")
paddle=PaddleOCRProvider(lang="en"); rows=[]; P=[]; T=[]
for i,(title,u) in enumerate(fetch_urls()):
    img=dl(u)
    if img is None: rows.append((title,"download failed","","")); continue
    if img.width>1280: img=img.resize((1280,round(img.height*1280/img.width)))
    fp=CORPUS/f"shot_{i:02d}.png"; img.save(fp)
    pl=paddle._predict(fp).lines
    ptext=" ".join(l.text for l in pl); pconf=sum((l.confidence or 0) for l in pl)/max(1,len(pl)); pw=wordy(ptext)[1]
    tt=subprocess.run(["tesseract",str(fp),"stdout"],capture_output=True,text=True,timeout=90).stdout
    tn,twd=wordy(tt); P.append(pw); T.append(twd)
    rows.append((title,f"{len(pl):>3}/{pconf:.2f}/{pw:.0%}",f"{tn:>4}/{twd:.0%}",ptext[:72]))
    time.sleep(0.8)

print(f"\n{'screenshot':<30}{'PADDLE ln/conf/wordy':<22}{'TESS wd/wordy':<14}")
print("-"*92)
for t,pc,tc,s in rows:
    print(f"{t[:28]:<30}{pc:<22}{tc:<14}")
    if s.strip(): print(f"    paddle: {s!r}")
print("-"*92)
if P: print(f"AVG real-word ratio over {len(P)} screenshots:  PaddleOCR {st.mean(P):.0%}   vs   full-frame Tesseract {st.mean(T):.0%}")
