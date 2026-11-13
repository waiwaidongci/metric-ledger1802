from collections import OrderedDict

from fastapi import HTTPException
from ..models import SourceRow,Metric
from ..utils.serialize import record
from ..utils.checksum import rows_checksum
from .pivot_service import build_report
def source_preview(db):
    return [record(x) for x in db.query(SourceRow).order_by(SourceRow.id).limit(100)]
def reconciliation(db):
    try:return build_report(db.query(SourceRow).order_by(SourceRow.id).all(),db.query(Metric).order_by(Metric.id).all(),["channel","month"])
    except ValueError as e:raise HTTPException(422,str(e))
def scope_summary(db):
    scopes=OrderedDict()
    for row in db.query(SourceRow).order_by(SourceRow.id).all():
        key=(row.channel,row.day[:7])
        item=scopes.setdefault(key,{"channel":row.channel,"month":row.day[:7],"row_count":0,"reference_net":0.0,"canonical_net":0.0})
        item["row_count"]+=1;item["reference_net"]+=row.reference_net;item["canonical_net"]+=row.revenue-row.refunds
    return sorted([{**item,"reference_net":round(item["reference_net"],6),"canonical_net":round(item["canonical_net"],6)} for item in scopes.values()],key=lambda x:(x["month"],x["channel"]))
def scope_preview(db,channel,month):
    rows=db.query(SourceRow).filter(SourceRow.channel==channel,SourceRow.day.like(month+"-%")).order_by(SourceRow.source_key).all()
    return [record(x) for x in rows]
def scope_checksum(db,channel,month):
    return rows_checksum(scope_preview(db,channel,month))
