export async function request<T>(path:string,options:RequestInit={}):Promise<T>{const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json','Authorization':'Bearer '+localStorage.getItem('metric-token'),...options.headers}});if(!response.ok){const body=await response.json().catch(()=>({detail:'网络异常'}));throw new Error(errorMessage(body.detail));}return response.json();}
function errorMessage(detail:unknown):string{
  if(typeof detail==='string')return detail;
  if(Array.isArray(detail)){
    const fields=detail.map((item:{loc?:unknown[];msg?:string})=>{
      const where=Array.isArray(item.loc)?item.loc.filter(part=>typeof part==='string'||typeof part==='number').slice(1).join('.'):'';
      return (where?where+'：':'')+(item.msg||'输入不合法');
    });
    return fields.join('；')||'请检查输入字段';
  }
  if(detail&&typeof detail==='object'){
    const body=detail as {message?:string;errors?:{row:number;field:string;message:string}[]};
    const head=body.message||'请求被拒绝';
    if(!body.errors?.length)return head;
    const lines=body.errors.map(error=>`第 ${error.row} 行${error.field&&error.field!=='-'?`（${error.field}）`:''}：${error.message}`);
    return head+'：'+lines.join('；');
  }
  return '请检查输入字段';
}
export async function download(path:string,name:string){const response=await fetch('/api'+path,{headers:{Authorization:'Bearer '+localStorage.getItem('metric-token')}});if(!response.ok)throw new Error('导出失败，请重试');const a=document.createElement('a');a.href=URL.createObjectURL(await response.blob());a.download=name;a.click();URL.revokeObjectURL(a.href);}
