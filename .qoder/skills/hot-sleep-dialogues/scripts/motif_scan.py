#!/usr/bin/env python3
"""motif_scan.py —— 段落级「母题重复」扫描（把「原地打转」变成可测量指标）。

用途：`audit_script.py` 只能查*整句*重复与 4-gram 近重复，查不出「同一件事换个说法讲 11 遍」。
本工具按 **4 段 × 50 对** 切分，逐段统计：

* 实词母题 top-N（去停用词后的词频）——同一段里某个词出现十几次，就是这一段在原地打转；
* **段间重叠度**（相邻段实词集合的 Jaccard）——高重叠说明两段在讲同一件事；
* b 句 Yes/No 起句占比、char_a 开头词分布——判断应答节拍是否单一。

用法：
    python motif_scan.py 034 035 ...          # 指定编号（三位或两位都行）
    python motif_scan.py --all --json         # 扫描库里所有 400 行脚本
    退出码：0 = 无超过阈值的段；2 = 有段超过阈值（review 级，不是 blocker）
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HOT = ROOT / 'ai_scripts_hot'
WORD_RE = re.compile(r"[A-Za-z'’]+|\d[\d.,]*")

STOP = set("""a an the and or but if so then than that this these those there here is are was were be been
being am do does did doing have has had having will would shall should can could may might must
i you he she it we they me him her us them my your his its our their mine yours hers ours theirs
to of in on at for with from by about into over after before under again very just really quite
not no yes okay ok sure please thanks thank sorry well now only also still even much many more
most some any all both each every other another one two three four five six seven eight nine ten
good great nice fine right wrong thing things something anything nothing everything someone anyone
what when where which who why how let us go get got make made take took put come came say said
see saw look looked know knew think thought want wanted need needed like liked time day days night
today tomorrow yesterday morning afternoon evening tonight""".split())


def folder_of(num):
    return next((p for p in HOT.iterdir() if p.is_dir() and p.name.startswith(str(num).zfill(3))), None)


def scan(folder, thr_word=12, thr_hot=25, thr_jac=0.45, thr_yes=0.5):
    """返回一篇的段落统计。

    thr_word : 逐段「提示级」阈值（只记 note，不判问题）
    thr_hot  : 「极端集中」阈值 —— 主题名词（pot/machine/plan）在**一个 50 对段**里出现这么多次
               才算该改写。基线：001–020 老批次里 007 pot×26 / 008 oil×25 / 017 gel×29 属常态，
               所以 <25 的词频是**主题固有**、不是缺陷。
    thr_jac  : 相邻段实词集合 Jaccard 阈值（高 = 两段在讲同一件事）
    thr_yes  : b 句 Yes/No 起句占比阈值
    """
    d = json.loads((folder / 'script.json').read_text(encoding='utf-8'))['dialogue']
    n_pairs = len(d) // 2
    seg_pairs = max(1, n_pairs // 4)
    out = {'folder': folder.name, 'pairs': n_pairs, 'segments': []}
    prev_words = None
    hot_max = 0
    for s in range(4):
        lo, hi = s * seg_pairs * 2, min(len(d), (s + 1) * seg_pairs * 2)
        blk = d[lo:hi]
        words = Counter()
        openers = Counter()
        yes = 0
        b_rows = 0
        for i, r in enumerate(blk):
            for w in WORD_RE.findall(r['text'].lower()):
                if w not in STOP and len(w) > 2:
                    words[w] += 1
            if i % 2 == 0:
                ws = WORD_RE.findall(r['text'])
                if ws:
                    openers[ws[0].lower()] += 1
            else:
                b_rows += 1
                if r['text'].startswith(('Yes', 'No', 'Sure', 'Exactly')):
                    yes += 1
        jac = None
        if prev_words:
            a, b = set(words), set(prev_words)
            jac = round(len(a & b) / max(1, len(a | b)), 3)
        prev_words = words
        hot = [w for w, c in words.most_common(6) if c >= thr_hot]
        note = [w for w, c in words.most_common(6) if thr_word <= c < thr_hot and w not in hot]
        hot_max = max([hot_max] + [c for _, c in words.most_common(6)])
        out['segments'].append({
            'seg': s + 1, 'lines': [lo, hi - 1],
            'top': words.most_common(8),
            'hot': hot, 'note': note,
            'jac_prev': jac,
            'jac_high': bool(jac is not None and jac >= thr_jac),
            'yes_ratio': round(yes / max(1, b_rows), 3),
            'yes_high': (yes / max(1, b_rows)) >= thr_yes,
            'openers': openers.most_common(4),
        })
    out['hot_max'] = hot_max
    out['flags'] = sum(1 for s in out['segments']
                       if s['hot'] or s['jac_high'] or s['yes_high'])
    out['notes'] = sum(1 for s in out['segments'] if s['note'])
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    as_json = '--json' in sys.argv
    rank = '--rank' in sys.argv
    if '--all' in sys.argv or not args:
        nums = sorted(int(p.name[:3]) for p in HOT.iterdir()
                      if p.is_dir() and p.name[:3].isdigit() and (p / 'script.json').exists())
    else:
        nums = [int(a) for a in args]
    results = []
    for n in nums:
        f = folder_of(n)
        if f is None or not (f / 'script.json').exists():
            continue
        results.append(scan(f))
    if rank:
        # 按「最热母题词频」排序：用来判断某篇相对库内基线是否真的异常
        for r in sorted(results, key=lambda x: -x['hot_max']):
            top = r['segments'][0]['top'][0] if r['segments'] else ('', 0)
            print('%-40s hot_max=%-3d flags=%d notes=%d  top1=%s×%d'
                  % (r['folder'], r['hot_max'], r['flags'], r['notes'], top[0], top[1]))
        return 0
    if as_json:
        print(json.dumps({'tool': 'motif_scan', 'root': str(HOT), 'checked': len(results),
                          'state': 'ok' if not any(r['flags'] for r in results) else 'review',
                          'summary': '%d scripts, %d segments flagged, %d notes'
                                     % (len(results), sum(r['flags'] for r in results),
                                        sum(r['notes'] for r in results)),
                          'results': results}, ensure_ascii=False, indent=1))
    else:
        for r in results:
            print('== %s  (%d 对)  hot_max=%d' % (r['folder'], r['pairs'], r['hot_max']))
            for s in r['segments']:
                marks = []
                if s['hot']:
                    marks.append('⛔极端集中 %s' % '/'.join('%s×%d' % (w, c) for w, c in s['top'] if w in s['hot']))
                if s['note']:
                    marks.append('·主题词偏多 %s' % '/'.join('%s×%d' % (w, c) for w, c in s['top'] if w in s['note']))
                if s['jac_high']:
                    marks.append('⛔与上段重叠 %s' % s['jac_prev'])
                if s['yes_high']:
                    marks.append('⛔Yes/No 起句 %d%%' % round(s['yes_ratio'] * 100))
                print('   段%d 行%4d-%-4d  top=%s' % (s['seg'], s['lines'][0], s['lines'][1],
                                                    ', '.join('%s×%d' % t for t in s['top'][:5])))
                if marks:
                    print('        ' + '  '.join(marks))
        print('\n合计 %d 篇 / %d 段被标记 / %d 段有提示' % (len(results), sum(r['flags'] for r in results),
                                                     sum(r['notes'] for r in results)))
    return 2 if any(r['flags'] for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
