/* Shared, read-only inspection UI. No signing keys or policy mutations in the browser. */
(() => {
  const visitor = location.pathname === '/demo/visitor';
  const api = visitor ? '/api/demo-cluster' : '/api/demo';
  const $ = s => document.querySelector(s);
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const titles = {Qualification:'Agent qualification', EthicsApproval:'Ethics approval', ComputeAuthorization:'Compute permission', DataAccessAuthorization:'Data permission'};
  const name = id => ({'urn:wasteland:demo:sakura-board':'Sakura Board', 'urn:wasteland:demo:sakura-analysis-lab':'Sakura Analysis Lab', 'urn:wasteland:demo:comparison-agent':'Analysis agent', 'urn:wasteland:demo:camelot:irb':'Camelot IRB', 'urn:wasteland:demo:visitor:data-owner':'Visitor / data owner'}[id] || id.replace('urn:wasteland:demo:', '').replaceAll(':', ' · '));
  const style = document.createElement('style');
  style.textContent = '.inspect-tools{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.evidence-dialog{max-width:1050px}.trust-cards{display:grid;grid-template-columns:1fr 1fr;gap:12px}.trust-card{border:1px solid var(--line);padding:15px;border-radius:6px}.trust-card h4{margin:0 0 8px}.trust-chain{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:12px 0}.trust-chain button{font:12px var(--mono);color:var(--mint)}.trust-card small{color:var(--muted)}.trust-card .pending{color:var(--sand)}.trust-card .verified{color:var(--mint)}.proof-detail{margin-top:16px;border-top:1px solid var(--line);padding-top:12px}.variant-scroll{max-height:55vh;overflow:auto}#variant-table{border-collapse:collapse;width:100%;font:12px var(--mono)}#variant-table td,#variant-table th{padding:8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}#variant-table th{position:sticky;top:0;background:var(--panel)}.evidence-dialog input{background:var(--ink);color:var(--text);border:1px solid var(--line);padding:9px;width:min(100%,420px)}@media(max-width:650px){.trust-cards{grid-template-columns:1fr}}';
  document.head.append(style);
  const tools = document.createElement('div');tools.className='inspect-tools';
  tools.innerHTML='<button class="quiet" id="inspect-trust">Inspect trust & public keys</button>';
  $('.actions').after(tools);
  const result = visitor ? $('#result') : $('#results');
  const resultTools = document.createElement('div');resultTools.className='inspect-tools';
  resultTools.innerHTML='<button class="quiet" id="view-all">View all variants</button><button class="quiet" id="download-all">Download all · TSV</button>';
  result.append(resultTools);
  document.body.insertAdjacentHTML('beforeend', '<dialog class="evidence-dialog" id="trust-dialog"><button class="quiet close" data-close="trust-dialog">Close</button><h2>Who vouches for whom?</h2><p>Each receiver chooses its trust roots. A signed accreditation can delegate a specific certification, within its scope and depth limit. Trust does not spread automatically to other permissions.</p><p id="trust-summary" role="status"></p><div class="inspect-tools"><button class="quiet" id="verify-signatures">Verify signatures in this browser</button><button class="quiet" id="tamper-proof">Test a tampered copy</button><button class="quiet" id="download-proof">Download public proof bundle</button></div><div id="trust-content"></div><section class="proof-detail" id="proof-detail" hidden><h3 id="proof-title"></h3><p id="proof-text"></p><details><summary>Full public key / signed document</summary><pre id="proof-json"></pre></details></section><p class="detail-meta">Real Ed25519 signatures, isolated demo authorities. Keys are generated per run. Status comes from the demo registry; this does not establish a real institution’s identity. Browser verification checks signatures; the receiver’s policy determines permission.</p></dialog><dialog class="evidence-dialog" id="all-variants"><button class="quiet close" data-close="all-variants">Close</button><h2>All computed variants</h2><p id="all-summary"></p><label for="variant-filter">Find a variant </label><input id="variant-filter" placeholder="Chromosome, position or allele"><div class="inspect-tools"><button class="quiet" id="download-table">Download all · TSV</button><button class="quiet" id="download-json">Download all · JSON</button></div><p id="filter-count" class="detail-meta"></p><div class="variant-scroll"><table id="variant-table"><thead></thead><tbody></tbody></table></div><p class="detail-meta">All released aggregate rows, not the underlying genotype file. Downloads include every row even when the view is filtered. Individual genotype export remains separately controlled.</p></dialog>');
  document.querySelectorAll('[data-close]').forEach(b => b.onclick=()=>$('#'+b.dataset.close).close());
  let bundle=null, dataset=null;
  async function get(path){const r=await fetch(path);if(!r.ok)throw Error('The stage is unavailable.');return r.json()}
  function save(filename,body,type){const url=URL.createObjectURL(new Blob([body],{type}));const a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000)}
  function details(title,text,data){$('#proof-detail').hidden=false;$('#proof-title').textContent=title;$('#proof-text').textContent=text;$('#proof-json').textContent=JSON.stringify(data,null,2);$('#proof-detail details').open=false;$('#proof-detail').scrollIntoView({block:'nearest'})}
  $('#inspect-trust').onclick=async()=>{
    $('#trust-dialog').showModal();$('#trust-summary').textContent='Reading current trust evidence…';$('#trust-content').replaceChildren();$('#proof-detail').hidden=true;bundle=null;
    try{bundle=await get(api+'/trust');if(!bundle.run_id){$('#trust-summary').textContent='Start this demo to create its signed requests and keychain.';return}
      $('#trust-summary').textContent='Policy rechecked now. Use browser verification to check every included signature locally. Expired credentials can still have valid signatures.';
      bundle.receivers.forEach(receiver=>{
        const section=document.createElement('section');section.innerHTML=`<h3>${escape(receiver.town)} · ${receiver.decision.ok?'permission accepted now':'permission incomplete or expired'}</h3><div class="trust-cards"></div>`;
        receiver.decision.requirements.forEach(req=>{
          const doc=receiver.presentation.credentials.find(d=>d.id===req.evidence?.credential)||receiver.presentation.credentials.find(d=>d.type.includes(req.type));
          const rule=receiver.policy.issuers.find(r=>r.types.includes(req.type));
          const root=req.evidence?.root||rule?.issuer, issuer=doc?.issuer;
          const card=document.createElement('article');card.className='trust-card';
          card.innerHTML=`<h4>${escape(titles[req.type]||req.type)}</h4><span class="${req.status==='pass'?'verified':'pending'}">${req.status==='pass'?'Accepted by receiver':'Not currently authorized'}</span><div class="trust-chain"></div><small>${escape(req.type==='Qualification'?'Root → accredited lab → certified agent. One delegation; aggregate analysis only.':'Receiver trusts this issuer for this permission only.')}</small><p><button class="quiet">Inspect signed evidence</button></p>`;
          const ids=[...new Set([root,issuer,receiver.presentation.holder].filter(Boolean))];
          ids.forEach((id,i)=>{if(i)card.querySelector('.trust-chain').append(' → ');const b=document.createElement('button');b.className='quiet';b.textContent=name(id);b.onclick=()=>{const key=receiver.keys.find(k=>k.id===id);details(name(id),`Public key fingerprint (SHA-256 of raw Ed25519 key): ${key?.fingerprint||'unavailable'}. Key source: ${key?.source||'unresolved'}.`,{key,receiver_trust_rule:id===root?rule:undefined,accreditations:receiver.presentation.accreditations.filter(d=>d.credentialSubject.id===id)})};card.querySelector('.trust-chain').append(b)});
          card.querySelector('p button').onclick=()=>details(titles[req.type],doc?`Signed by ${name(doc.issuer)}. Registry status: ${receiver.status[doc.id]}. Expires: ${doc.validUntil}. Scope: ${doc.credentialSubject.scope}.`:'No credential has been issued for this permission.',{credential:doc||null,decision:req,task:receiver.task,accreditations:receiver.presentation.accreditations,receiver_rule:rule});
          section.querySelector('.trust-cards').append(card);
        });$('#trust-content').append(section);
      });
    }catch(e){$('#trust-summary').textContent=e.message}
  };
  const decode=s=>Uint8Array.from(atob(s.replaceAll('-','+').replaceAll('_','/')+'='.repeat((4-s.length%4)%4)),c=>c.charCodeAt(0));
  function canonical(value){if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';return JSON.stringify(value)}
  async function verify(doc,publicText){
    const unsigned=Object.fromEntries(Object.entries(doc).filter(([k])=>k!=='proof'));
    const message=new TextEncoder().encode(canonical(unsigned)),signature=decode(doc.proof.proofValue),raw=decode(publicText.split(':')[1]);
    if(window.nacl)return nacl.sign.detached.verify(message,signature,raw);
    const key=await crypto.subtle.importKey('raw',raw,{name:'Ed25519'},false,['verify']);
    return crypto.subtle.verify('Ed25519',key,signature,message);
  }
  $('#verify-signatures').onclick=async()=>{
    if(!bundle?.run_id)return;let total=0,passed=0;$('#trust-summary').textContent='Verifying Ed25519 signatures locally…';
    try{for(const r of bundle.receivers){for(const doc of [...r.presentation.accreditations,...r.presentation.credentials,r.presentation]){const issuer=doc.issuer||doc.holder;const key=r.keys.find(k=>k.id===issuer);total++;if(key&&await verify(doc,key.public_key))passed++}}
      $('#trust-summary').textContent=`Browser verified ${passed}/${total} Ed25519 signatures. ${passed===total?'All signatures match.':'A signature failed; do not accept this evidence.'} Trust scope, expiry and status are separate receiver checks shown below.`;
    }catch(e){$('#trust-summary').textContent='This browser could not verify Ed25519: '+e.message+'. The downloadable proof bundle can be checked independently.'}
  };
  $('#tamper-proof').onclick=async()=>{if(!bundle?.run_id)return;try{const r=bundle.receivers[0],doc=structuredClone(r.presentation.credentials[0]);doc.credentialSubject.scope='unauthorized-export';const ok=await verify(doc,r.keys.find(k=>k.id===doc.issuer).public_key);$('#trust-summary').textContent=ok?'Unexpected verification result.':'Tampered copy rejected: changing the signed scope breaks its signature. The live credential and permissions were not changed.'}catch(e){$('#trust-summary').textContent=e.message}};
  $('#download-proof').onclick=()=>{if(bundle?.run_id)save('trust-'+bundle.run_id+'.json',JSON.stringify(bundle,null,2),'application/json')};
  async function loadRows(){const {run}=await get(api);if(!run?.result)throw Error('This demo has no released results yet.');dataset={run_id:run.id,region:run.region,cohorts:run.cohorts,input:run.input,result:run.result};return dataset}
  function tableData(){return visitor?{headers:['variant','alternate_alleles','called_alleles','frequency'],rows:dataset.result.rows.map(r=>[r.variant,r.ac,r.an,r.ac/r.an])}:{headers:['variant','saudi_alternate_alleles','saudi_called_alleles','saudi_frequency','japanese_alternate_alleles','japanese_called_alleles','japanese_frequency'],rows:dataset.result.rows.map(r=>[r.variant,r.ubar.ac,r.ubar.an,r.ubar.af,r.yamatai.ac,r.yamatai.an,r.yamatai.af])}}
  function renderTable(){const {headers,rows}=tableData(),q=$('#variant-filter').value.toLowerCase(),filtered=rows.filter(r=>String(r[0]).toLowerCase().includes(q));$('#variant-table thead').innerHTML='<tr>'+headers.map(h=>'<th>'+escape(h.replaceAll('_',' '))+'</th>').join('')+'</tr>';$('#variant-table tbody').innerHTML=filtered.map(row=>'<tr>'+row.map(v=>'<td>'+escape(v)+'</td>').join('')+'</tr>').join('');$('#filter-count').textContent=`Showing ${filtered.length} of ${rows.length} released variant rows.`}
  $('#view-all').onclick=async()=>{try{await loadRows();$('#variant-filter').value='';$('#all-summary').textContent=`${dataset.result.rows.length} variants from this completed analysis. Frequencies are alternate / called alleles; missing alleles are excluded.`;renderTable();$('#all-variants').showModal()}catch(e){alert(e.message)}};
  $('#variant-filter').oninput=renderTable;
  async function downloadTSV(){try{await loadRows();const {headers,rows}=tableData();save((visitor?'visitor-':'cohorts-')+dataset.run_id+'.tsv',[headers,...rows].map(r=>r.join('\t')).join('\n')+'\n','text/tab-separated-values')}catch(e){alert(e.message)}}
  $('#download-all').onclick=$('#download-table').onclick=downloadTSV;
  $('#download-json').onclick=async()=>{await loadRows();save('aggregates-'+dataset.run_id+'.json',JSON.stringify(dataset,null,2),'application/json')};
})();
