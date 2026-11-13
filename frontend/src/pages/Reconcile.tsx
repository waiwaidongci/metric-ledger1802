import {ChangeEvent,useEffect,useMemo,useRef,useState} from 'react';
import {FileUp,Download,CheckCircle2,AlertTriangle,ArrowRight} from 'lucide-react';
import {useLedgerData} from '../hooks/useLedger';
import {useAuth} from '../stores/auth';
import {useAction} from '../hooks/useAction';
import {previewBatch,confirmBatch,getScopeRows} from '../api/sources';
import {download} from '../api/client';
import {Notice} from '../components/common/Notice';
import {DataTable} from '../components/common/DataTable';
import {ReactNode} from 'react';
import {BatchApplied,BatchPreview,SourceRow} from '../types/domain';
import {money,number,percent,time} from '../utils/format';

const FIELD_LABELS:Record<string,string>={revenue:'收入',cost:'成本',refunds:'退款',orders:'单数',reference_net:'参考净收入',day:'日期',region:'区域',channel:'渠道'};
const AMOUNT_FIELDS=new Set(['revenue','cost','refunds','reference_net']);
const METRIC_LABELS:Record<string,string>={net_revenue:'净收入',gross_profit:'贡献毛利',margin_rate:'毛利率',net_per_order:'每单净收入',refund_rate:'退款率'};

function formatValue(field:string,value:string|number){
  if(typeof value==='number'&&AMOUNT_FIELDS.has(field))return number(value);
  if(typeof value==='number'&&field==='orders')return number(value,0);
  return String(value);
}
function formatDelta(delta:number|undefined,ratio=false){
  if(delta===undefined)return '—';
  const text=(delta>0?'+':'')+ (ratio?number(delta,4):number(delta));
  return text;
}

export function Reconcile(){
  const s=useLedgerData(),{user}=useAuth(),action=useAction();
  const canWrite=user?.role==='admin'||user?.role==='analyst';
  const [channel,setChannel]=useState('');
  const [month,setMonth]=useState('');
  const [file,setFile]=useState<File|null>(null);
  const [preview,setPreview]=useState<BatchPreview|null>(null);
  const [applied,setApplied]=useState<BatchApplied|null>(null);
  const [scopeRows,setScopeRows]=useState<SourceRow[]>([]);
  const [scopeError,setScopeError]=useState('');
  const [pendingConfirm,setPendingConfirm]=useState(false);
  const inputRef=useRef<HTMLInputElement>(null);
  const currentScope=useMemo(()=>s.scopes.find(item=>item.channel===channel&&item.month===month),[s.scopes,channel,month]);

  useEffect(()=>{
    if(!s.scopes.length)return;
    const stillValid=s.scopes.some(item=>item.channel===channel&&item.month===month);
    if(stillValid)return;
    const latest=s.scopes[s.scopes.length-1];
    setChannel(latest.channel);setMonth(latest.month);
    setPreview(null);setApplied(null);
  },[s.scopes,channel,month]);

  useEffect(()=>{
    if(!channel||!month)return;
    let live=true;setScopeError('');
    getScopeRows(channel,month).then(rows=>{if(live)setScopeRows(rows)}).catch(error=>{if(live)setScopeError((error as Error).message)});
    return()=>{live=false};
  },[channel,month,applied,preview]);

  function chooseFile(event:ChangeEvent<HTMLInputElement>){
    setFile(event.target.files?.[0]||null);setPreview(null);setApplied(null);
  }
  function resetBatch(preserveResult=false){
    setFile(null);setPreview(null);if(!preserveResult)setApplied(null);if(inputRef.current)inputRef.current.value='';
  }

  function runPreview(){
    action.execute(async()=>{
      if(!channel||!month)throw new Error('请先选择渠道与月份');
      if(!file)throw new Error('请选择要替换的 CSV 明细');
      const text=await file.text();
      const result=await previewBatch({channel,month,content:text});
      setPreview(result);setApplied(null);
    });
  }

  function runConfirm(){
    if(!preview)return;
    action.execute(async()=>{
      try{
        const result=await confirmBatch(preview.batch_id);
        setApplied(result);setPreview(null);setPendingConfirm(false);resetBatch(true);await s.refresh();
      }catch(error){
        setPendingConfirm(false);
        // 旧批次过期时保留错误信息；刷新后重新选择再预览。
        await s.refresh();
        throw error;
      }
    });
  }

  function selectScope(nextChannel:string,nextMonth:string){
    setChannel(nextChannel);setMonth(nextMonth);resetBatch();
  }

  return <div className="page">
    <div className="page-title"><div><p className="eyebrow">SOURCE RECONCILIATION / 03</p><h1>来源核对<span>差异不只给总数，也要找到落点；整月更正要先看见变化。</span></h1></div><button className="outline" onClick={()=>action.execute(async()=>{await s.refresh()})}>重新核对</button></div>
    <Notice message={s.error||action.error||scopeError} retry={()=>action.execute(async()=>{await s.refresh()})}/>

    {applied&&<div className="batch-result" role="status">
      <CheckCircle2 size={19}/>
      <div>
        <b>{applied.already_applied?'该批次此前已确认，本次未重复入库。':'整月更正已生效。'}</b>
        <span>{applied.channel} · {applied.month} · 新批 {applied.row_count} 条 · 范围版本 v{applied.scope_version}</span>
        <small className="mono">批次 #{applied.batch_id} / SHA256 {applied.checksum.slice(0,24)}…</small>
      </div>
    </div>}

    <div className="scope-bar">
      <label>渠道与月份范围
        <select aria-label="更正渠道" value={channel} onChange={event=>selectScope(event.target.value,month)}>
          {Array.from(new Set(s.scopes.map(item=>item.channel))).map(item=><option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label>月份
        <select aria-label="更正月份" value={month} onChange={event=>selectScope(channel,event.target.value)}>
          {s.scopes.filter(item=>item.channel===channel).map(item=><option key={item.month} value={item.month}>{item.month}</option>)}
        </select>
      </label>
      <div className="scope-meta">
        <span>范围内现有 <b>{currentScope?.row_count??0}</b> 条</span>
        <span>参考账本 {money(currentScope?.reference_net)}</span>
        <span>来源净收入 {money(currentScope?.canonical_net)}</span>
      </div>
      <button className="outline" disabled={!channel||!month} onClick={()=>action.execute(async()=>{await download(`/source-batches/${encodeURIComponent(channel)}/${month}/export`,`source-${channel}-${month}.csv`)})}><Download size={15}/>下载当前明细 / 模板</button>
    </div>

    {canWrite?<Section title="上传整月替换明细" note="完整替换该渠道该月的来源；确认前只做预览，不写入来源。">
      <div className="upload-row">
        <label className="file-field">选择 CSV 文件
          <input ref={inputRef} aria-label="替换明细 CSV" type="file" accept=".csv,text/csv" onChange={chooseFile}/>
        </label>
        <button className="primary" disabled={!file||action.busy} onClick={()=>runPreview()}>预览增减与差额 <ArrowRight size={15}/></button>
        <button className="outline" onClick={()=>resetBatch()} disabled={action.busy||(!file&&!preview)}>清空</button>
      </div>
      {!file&&<p className="caption">列顺序：source_key, day, channel, region, revenue, cost, refunds, orders, reference_net。上传编号不得与其它渠道/月份重复；坏行、跨范围、重复编号或非法金额会整批拒绝。</p>}
      {file&&<p className="caption">已选择 <b>{file.name}</b> · {number(file.size/1024,1)} KB。<span className="mono">{file.type||'text/csv'}</span></p>}

      {preview&&<BatchPreviewPanel preview={preview} pending={pendingConfirm} onConfirm={()=>{setPendingConfirm(true);runConfirm()}} onDiscard={()=>{setPreview(null);setApplied(null)}} busy={action.busy}/>}
    </Section>:<Section title="整月替换明细" note="当前角色可以查看核对结果与替换记录，替换由口径负责人或经营分析师确认。">
      <p className="role-note">来源只读。若需更正，请由口径负责人或经营分析师上传完整明细并预览确认。</p>
    </Section>}

    <div className="section-heading"><h2>差异归因</h2><span>按绝对差异排序 / 阈值 0.01 元</span></div>
    <DataTable head={['渠道','月份','来源重算 / 元','参考账本 / 元','差异 / 元','相对差异','明细数']}>
      {s.reconciliation?.attribution.length?s.reconciliation.attribution.map((cell,i)=><tr key={i}><td>{cell.dimensions.channel}</td><td>{cell.dimensions.month}</td><td>{number(cell.canonical_net)}</td><td>{number(cell.reference_net)}</td><td className="difference">{number(cell.difference)}</td><td>{percent(cell.relative_difference)}</td><td>{cell.source_count}</td></tr>):<tr><td colSpan={7}>本次无超阈值差异，可继续生成交付报表。</td></tr>}
    </DataTable>
    <p className="caption">{s.reconciliation?.reconciliation_rule}</p>

    <div className="section-heading"><h2>当前范围明细</h2><span>{channel} · {month} / 共 {scopeRows.length} 条{scopeRows.length>100?' · 显示前100条':''}</span></div>
    <DataTable head={['来源编号','日期','区域','收入 / 元','退款 / 元','成本 / 元','单数','参考净收入']}>
      {scopeRows.slice(0,100).map(row=><tr key={row.source_key}><td className="mono">{row.source_key}</td><td>{row.day}</td><td>{row.region}</td><td>{number(row.revenue)}</td><td>{number(row.refunds)}</td><td>{number(row.cost)}</td><td>{row.orders}</td><td>{number(row.reference_net)}</td></tr>)}
      {!scopeRows.length&&<tr><td colSpan={8}>该范围暂无来源明细。</td></tr>}
    </DataTable>

    <div className="section-heading"><h2>替换批次记录</h2><span>最近 {s.batches.length} 批</span></div>
    <DataTable head={['批次','渠道 · 月份','状态','新增 / 删除 / 改值','净收入差额 / 元','提交人','时间']}>
      {s.batches.map(batch=><tr key={batch.batch_id}><td className="mono">#{batch.batch_id}</td><td>{batch.channel} · {batch.month}</td><td><span className={'batch-status '+batch.status}>{batch.status==='applied'?'已生效':'预览未确认'}</span></td><td>{batch.counts.added??'—'} / {batch.counts.removed??'—'} / {batch.counts.changed??'—'}</td><td className={(batch.net_revenue_delta??0)>0?'difference':''}>{batch.net_revenue_delta==null?'—':formatDelta(batch.net_revenue_delta)}</td><td>{batch.created_by}</td><td>{time(batch.created_at)}</td></tr>)}
      {!s.batches.length&&<tr><td colSpan={7}>还没有上传过替换批次。</td></tr>}
    </DataTable>
  </div>;
}

function Section({title,note,children}:{title:string;note:string;children:ReactNode}){
  return <section className="batch-section"><div className="section-heading top"><h2>{title}</h2><span>{note}</span></div>{children}</section>;
}

function BatchPreviewPanel({preview,pending,onConfirm,onDiscard,busy}:{preview:BatchPreview;pending:boolean;onConfirm:()=>void;onDiscard:()=>void;busy:boolean}){
  const deltaPositive=preview.net_revenue_delta>0;
  return <div className="batch-preview">
    <div className="preview-head">
      <div><span className="eyebrow">PREVIEW / 尚未生效</span><h2>{preview.scope.channel} · {preview.scope.month}</h2><p>新批 {preview.row_count} 条 / 当前 {preview.counts.existing} 条 · 范围版本 v{preview.scope.version} · <span className="mono">{preview.checksum.slice(0,20)}…</span></p></div>
      <div className="preview-net"><span>净收入差额</span><strong className={deltaPositive?'positive':'difference'}>{formatDelta(preview.net_revenue_delta)}</strong><small>确认后核对与新报表使用完整新批</small></div>
    </div>

    <div className="preview-counts">
      <div><span>新增</span><b>{preview.counts.added}</b></div>
      <div><span>删除</span><b>{preview.counts.removed}</b></div>
      <div><span>改值</span><b>{preview.counts.changed}</b></div>
      <div><span>未变</span><b>{preview.counts.unchanged}</b></div>
    </div>

    <div className="preview-totals">
      <DataTable head={['合计项','当前 / 元','新批 / 元','差额 / 元']}>
        {Object.entries(preview.totals.delta).filter(([field])=>field!=='difference'||Math.abs(preview.totals.after.difference-preview.totals.before.difference)>1e-9).map(([field,value])=><tr key={field}><td>{FIELD_LABELS[field]||field}</td><td>{number(preview.totals.before[field])}</td><td>{number(preview.totals.after[field])}</td><td className={Number(value)!==0?'difference':''}>{formatDelta(Number(value))}</td></tr>)}
      </DataTable>
    </div>

    {preview.metrics.before&&<div className="preview-totals"><DataTable head={['口径指标','当前','新批','差额']}>
      {Object.keys(preview.metrics.after||{}).map(key=>{const before=preview.metrics.before?.[key];const after=preview.metrics.after?.[key];const delta=preview.metrics.delta?.[key];const ratio=key==='margin_rate'||key==='refund_rate';return <tr key={key}><td>{METRIC_LABELS[key]||key}</td><td>{before==null?'—':number(before,ratio?4:2)}</td><td>{after==null?'—':number(after,ratio?4:2)}</td><td className={(delta??0)!==0?'difference':''}>{formatDelta(delta,ratio)}</td></tr>})}
    </DataTable></div>}

    <div className="section-heading"><h2>增减明细</h2><span>{preview.truncated?'每类最多显示500条':`新增 ${preview.counts.added} · 删除 ${preview.counts.removed} · 改值 ${preview.counts.changed}`}</span></div>
    <DataTable head={['动作','来源编号','日期','区域','收入 / 元','退款 / 元','成本 / 元','单数','参考净收入']}>
      {preview.added.map(row=><tr key={'a-'+row.source_key}><td><span className="badge add">新增</span></td><td className="mono">{row.source_key}</td><td>{row.day}</td><td>{row.region}</td><td>{number(row.revenue)}</td><td>{number(row.refunds)}</td><td>{number(row.cost)}</td><td>{row.orders}</td><td>{number(row.reference_net)}</td></tr>)}
      {preview.removed.map(row=><tr key={'r-'+row.source_key}><td><span className="badge remove">删除</span></td><td className="mono">{row.source_key}</td><td>{row.day}</td><td>{row.region}</td><td>{number(row.revenue)}</td><td>{number(row.refunds)}</td><td>{number(row.cost)}</td><td>{row.orders}</td><td>{number(row.reference_net)}</td></tr>)}
      {!preview.added.length&&!preview.removed.length&&<tr><td colSpan={9}>没有新增或删除的来源编号。</td></tr>}
    </DataTable>

    <div className="section-heading"><h2>数值变化</h2><span>逐字段对比当前来源与新批</span></div>
    <DataTable head={['来源编号','字段','当前','新批','差额']}>
      {preview.changed.length?preview.changed.map(row=>row.changes.map((change,index)=><tr key={row.source_key+change.field}><td className="mono">{index===0?row.source_key:'↳'}</td><td>{FIELD_LABELS[change.field]||change.field}</td><td>{formatValue(change.field,change.before)}</td><td>{formatValue(change.field,change.after)}</td><td className={(change.delta??0)!==0?'difference':''}>{change.delta===undefined?'—':formatDelta(change.delta)}</td></tr>)):<tr><td colSpan={5}>没有需要改值的来源。</td></tr>}
    </DataTable>

    <div className="confirm-row">
      <p>{preview.reconciliation_rule}</p>
      <div>
        <button className="outline" onClick={onDiscard} disabled={busy}>放弃本批</button>
        <button className="primary" onClick={onConfirm} disabled={busy||pending}>{pending?'正在确认…':'确认生效，整批替换'}</button>
      </div>
    </div>
    {pending&&<p className="caption"><AlertTriangle size={13}/> 确认时会再次核对来源版本；若预览后来源已变化，本批会被拒绝，需要重新预览。</p>}
  </div>;
}
