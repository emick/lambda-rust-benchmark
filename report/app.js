'use strict';
const data = JSON.parse(document.getElementById('benchmark-data').textContent);
const variants = ['python','python-snapstart','rust'];
const names = {'python':'Python','python-snapstart':'Python + SnapStart','rust':'Rust'};
const colors = {'python':'var(--python)','python-snapstart':'var(--snap)','rust':'var(--rust)'};
const labels = {health:'Framework · GET /health',validate:'Validation · valid JSON',validate_invalid:'Validation · rejected JSON',users:'DynamoDB · POST /users',files:'S3 · PUT /files (1 KB)'};
const $ = id => document.getElementById(id);
const esc = value => String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n = value => value == null ? '—' : Number(value).toFixed(2);
const money = value => value == null ? '—' : '$'+Number(value).toFixed(4);
const perRequest = value => value == null ? '—' : '$'+Number(value).toFixed(9);
function table(head,rows){return `<div class="table-scroll"><table><thead><tr>${head.map(x=>`<th scope="col">${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(x=>`<td>${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;}
function compare(){
 const memory=Number($('memory').value),metric=$('metric').value;
 let sections=[];
 for(const scenario of Object.keys(labels))for(const phase of ['cold','warm']){
  if(phase==='cold'&&scenario.startsWith('validate'))continue;
  const groups=variants.map(v=>data.groups.find(g=>g.variant===v&&g.memory_mb===memory&&g.scenario===scenario&&g.phase===phase));
  const rows=groups.map((g,i)=>{const s=g?.metrics[metric];return [names[variants[i]],`${g?.accepted||0} / ${g?.required||0}`,s?.count||0,n(s?.p50),n(s?.p90),n(s?.p95),n(s?.p99),n(s?.mean),n(s?.min),n(s?.max),n(s?.stddev),s?.outliers_iqr??'—',g?.errors||0,g?.throttles||0];});
  const max=Math.max(1,...groups.map(g=>g?.metrics[metric]?.p95||0));
  const bars=groups.map((g,i)=>{const s=g?.metrics[metric];return `<div class="bar-row"><span>${names[variants[i]]}</span><div class="bar-track"><div class="bar" style="width:${s?100*s.p95/max:0}%;background:${colors[variants[i]]}"></div></div><span class="value">${n(s?.p95)} ${metric==='max_memory_mb'?'MB':'ms'}</span></div>`;}).join('');
  const rust=groups[2]?.metrics[metric];
  const deltas=groups.slice(0,2).map((g,i)=>{const s=g?.metrics[metric];if(!s||!rust)return `Rust vs ${names[variants[i]]}: unavailable`;const d=rust.p95-s.p95;return `Rust − ${names[variants[i]]} p95: ${d>=0?'+':''}${n(d)} ${metric==='max_memory_mb'?'MB':'ms'} (${s.p95?n(100*d/s.p95)+'%':'undefined'})`;}).join(' · ');
  sections.push(`<article class="comparison"><h3>${labels[scenario]} / ${phase==='cold'?(scenario==='health'?'cold startup / restore':'fresh environment · first service use'):'warm reuse'}</h3><div class="bars" aria-label="p95 comparison">${bars}</div>${table(['Variant','Accepted / target','Metric n','p50','p90','p95','p99','Mean','Min','Max','Std dev','Outliers','Errors','HTTP 429'],rows)}<p class="deltas">${deltas}</p></article>`);
 }
 $('comparisons').innerHTML=sections.join('');
 costTable();
}
function costTable(){
 const memory=Number($('memory').value),volume=Number($('volume').value),cold=Number($('cold').value);
 const selected=data.costs.filter(c=>c.memory_mb===memory&&c.requests===volume&&c.cold_fraction===cold);
 const rows=selected.map(c=>[names[c.variant],money(c.lambda_requests),money(c.lambda_duration),money(c.snapshot_caching),money(c.snapshot_restoration),money(c.snapshot_initialization),money(c.lambda_total),perRequest(c.lambda_per_request),money(c.lambda_per_million),money(c.services.api_gateway),money(c.services.dynamodb_writes+c.services.dynamodb_storage),money(c.services.s3_puts+c.services.s3_storage),money(c.grand_total)]);
 $('cost-table').innerHTML=rows.length?table(['Variant','Requests','Duration','Cache','Restore','Init','Lambda/month','Lambda/request','Lambda/million','API Gateway','DynamoDB','S3','Total/month'],rows):'<p class="empty">Cost projections require verified billed durations for the cold and warm scenarios and a pricing snapshot.</p>';
 const rust=selected.find(c=>c.variant==='rust');
 if(rust){const deltas=selected.filter(c=>c.variant!=='rust').map(c=>`Rust − ${names[c.variant]} Lambda/month: ${money(rust.lambda_total-c.lambda_total)} (${n(100*(rust.lambda_total-c.lambda_total)/c.lambda_total)}%)`);$('cost-table').innerHTML+=`<p class="deltas">${deltas.join(' · ')}</p>`;}
}
$('memory').addEventListener('change',compare);$('metric').addEventListener('change',compare);
$('volume').addEventListener('change',costTable);$('cold').addEventListener('change',costTable);
$('audit').textContent=data.audit.complete?'All supported cells meet SPEC sample targets; functional equivalence and regional client location are verified.':`${data.audit.incomplete.length} measurement groups have unmet sample targets. Functional equivalence: ${data.audit.functional_ok?'verified':'incomplete'}. Regional pricing: ${data.audit.pricing_ok?'captured':'missing'}. Run status: ${data.metadata.status||'unavailable'}. Unsupported configurations: ${data.audit.unsupported.length}.`;
$('audit').classList.toggle('incomplete',!data.audit.complete);
$('missing').textContent=JSON.stringify({incomplete:data.audit.incomplete,unsupported:data.audit.unsupported},null,2);
$('pricing-note').textContent=data.pricing?`USD regional public prices captured ${data.pricing.date}. Free Tier and discounts excluded. Select a request volume and restore frequency.`:'No current pricing snapshot is available; cost estimates are withheld.';
$('all-costs').innerHTML=table(['Variant','Memory MB','Requests/month','Cold %','Lambda/month','API Gateway','DynamoDB','S3','Total/month'],data.costs.map(c=>[names[c.variant],c.memory_mb,c.requests,100*c.cold_fraction,money(c.lambda_total),money(c.services.api_gateway),money(c.services.dynamodb_writes+c.services.dynamodb_storage),money(c.services.s3_puts+c.services.s3_storage),money(c.grand_total)]));
const resources=variants.map(v=>{const g=data.groups.filter(g=>g.variant===v&&g.accepted);const memories=(data.metadata.cells||[]).filter(c=>c.variant===v&&c.status==='complete').map(c=>c.memory_mb);const max=Math.max(0,...g.map(g=>g.metrics.max_memory_mb?.max||0));const deployed=data.metadata.deployed?.[v];return [names[v],memories.length?Math.min(...memories)+' MB':'unverified',max?n(max)+' MB':'—',deployed?.CodeSize??data.metadata.build?.artifacts?.[v==='rust'?'rust':'python']?.bytes??'—',deployed?.Runtime??'—',deployed?.CodeSha256??'—'];});
$('resource-table').innerHTML=table(['Variant','Minimum successful allocation','Peak observed memory','ZIP bytes','Runtime','Deployed code hash'],resources);
$('functional').innerHTML=table(['Variant','Memory MB','Passing checks','Total checks'],(data.metadata.functional||[]).map(f=>[names[f.variant],f.memory_mb,f.checks.filter(c=>c.passed).length,f.checks.length]));
$('publication-init').innerHTML=table(['Publication initialization','Memory MB','n','p50 ms','p95 ms','p99 ms','Mean ms','Std dev ms'],(data.publish_initialization||[]).filter(g=>g.statistics).map(g=>[names[g.variant],g.memory_mb,g.statistics.count,n(g.statistics.p50),n(g.statistics.p95),n(g.statistics.p99),n(g.statistics.mean),n(g.statistics.stddev)]));
$('accounting').innerHTML=table(['Accounting','Value'],Object.entries(data.metadata.traffic||{}).map(([k,v])=>[k,typeof v==='object'?JSON.stringify(v):v]));
if(data.evidence_audit){const audit=data.evidence_audit;$('evidence-audit').innerHTML=`Raw evidence audit: ${audit.checks.filter(c=>c.passed).length}/${audit.checks.length} checks pass. <a href="acceptance.json" download>Download requirement checks and source hashes ↓</a>`;}
const publications=data.metadata.publications||[];
const cacheRate=data.pricing?.rates?.snap_cache_gb_s?.usd||0;
const cleanup=data.metadata.final_cleanup;
const cacheEnd=cleanup?.verified?Date.parse(cleanup.completed_at):Date.now();
const churn=publications.filter(p=>p.variant==='python-snapstart').reduce((sum,p)=>sum+p.memory_mb/1024*Math.max(10800,((p.deleted_at?Date.parse(p.deleted_at):cacheEnd)-Date.parse(p.published_at))/1000)*cacheRate,0);
$('churn').textContent=`Benchmark version churn: ${publications.length} fresh versions published, ${publications.filter(p=>p.deleted_at).length} deleted at export time. SnapStart caching for measured publications is approximately ${money(churn)}; ${cleanup?.verified?'remaining versions were removed with their stacks, using the verified cleanup time as a deletion upper bound':'open versions continue accruing charges until cleanup'}. This excludes the initial CDK-created version and service-managed reinitializations. Production projections above use one retained version.`;
if(cleanup){const link=document.createElement('a');link.href='cleanup.json';link.download='cleanup.json';link.textContent='AWS cleanup receipt ↓';document.querySelector('.downloads').append(link);}
compare();
