from urllib.parse import quote

from fastapi import APIRouter,Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session
from ..config.database import get_db
from ..controllers.dependencies import current_user,roles
from ..constants.roles import Role
from ..schemas.source_batch import BatchAppliedOut,BatchInput,BatchPreviewOut
from ..services.source_batch_service import apply_batch,list_batches,preview_batch,scope_csv
router=APIRouter(prefix="/source-batches",tags=["source-batches"])
# 来源只读；整月替换仅口径负责人与经营分析师可预览/确认。
require_writer=roles(Role.ADMIN,Role.ANALYST)


@router.get("")
def index(db:Session=Depends(get_db),user=Depends(current_user)):return list_batches(db)


@router.post("/preview",response_model=BatchPreviewOut)
def preview(data:BatchInput,db:Session=Depends(get_db),user=Depends(require_writer)):return preview_batch(db,data,user)


@router.post("/{batch_id}/confirm",response_model=BatchAppliedOut)
def confirm(batch_id:int,db:Session=Depends(get_db),user=Depends(require_writer)):return apply_batch(db,batch_id,user)


@router.get("/{channel}/{month}/export")
def export(channel:str,month:str,db:Session=Depends(get_db),user=Depends(current_user)):
    filename=quote(f"source-{channel}-{month}.csv")
    return Response(content="﻿"+scope_csv(db,channel,month),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename*=UTF-8''{filename}"})
