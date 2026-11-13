import {request} from './client';import {BatchApplied,BatchPreview,BatchSummary,ReportResult,SourceRow,SourceScopeSummary} from '../types/domain';
export const getSources=()=>request<SourceRow[]>('/sources');
export const getReconciliation=()=>request<ReportResult>('/sources/reconciliation');
export const getScopes=()=>request<SourceScopeSummary[]>('/sources/scopes');
export const getScopeRows=(channel:string,month:string)=>request<SourceRow[]>(`/sources/scope/${encodeURIComponent(channel)}/${month}`);
export const previewBatch=(data:{channel:string;month:string;content?:string;rows?:Record<string,unknown>[]})=>request<BatchPreview>('/source-batches/preview',{method:'POST',body:JSON.stringify(data)});
export const confirmBatch=(batchId:number)=>request<BatchApplied>(`/source-batches/${batchId}/confirm`,{method:'POST'});
export const getBatches=()=>request<BatchSummary[]>('/source-batches');
