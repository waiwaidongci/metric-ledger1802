from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from ..config.database import get_db
from ..controllers.dependencies import current_user
from ..schemas.source import SourceOutput
from ..services.source_service import source_preview,reconciliation,scope_summary,scope_preview
router=APIRouter(prefix="/sources",tags=["sources"])


@router.get("",response_model=list[SourceOutput])
def index(db:Session=Depends(get_db),user=Depends(current_user)):return source_preview(db)


@router.get("/scopes")
def scopes(db:Session=Depends(get_db),user=Depends(current_user)):return scope_summary(db)


@router.get("/scope/{channel}/{month}",response_model=list[SourceOutput])
def scope(channel:str,month:str,db:Session=Depends(get_db),user=Depends(current_user)):return scope_preview(db,channel,month)


@router.get("/reconciliation")
def check(db:Session=Depends(get_db),user=Depends(current_user)):return reconciliation(db)
