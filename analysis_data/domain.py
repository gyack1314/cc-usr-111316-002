"""仅核对输入形状，不生成分析结果。"""
from datetime import date
from .io import unique
def summarize(v,root):
    unique(v['tools'],'id');unique(v['scenarios'],'id');date.fromisoformat(v['valuation_date'])
    for t in v['tools']:
        for d in t['disbursements']:date.fromisoformat(d['date'])
    return {'tools':len(v['tools']),'scenarios':len(v['scenarios']),'channel_edges':len(v['channel_shares'])}
