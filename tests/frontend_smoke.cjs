'use strict';
// Focused local tests for the browser UI. All research records below are synthetic.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'ui.html'), 'utf8');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
const executable = scripts.at(-1)[1].replace(/\}\)\(\);\s*$/, 'globalThis.__test={setState,rowFor,selectedSummary,publicationRows,facultyDataStatus,httpsLink,doiLink,esc,showPublication,pollStatus,changeTab};})();');

class Element {
  constructor(id='', classes='') {
    this.id=id;this.value='';this.innerHTML='';this.textContent='';this.disabled=false;this.style={};this.dataset={};this.listeners={};this.attributes={};this.children=[];
    const values=new Set(classes.split(/\s+/).filter(Boolean));
    this.classList={add:v=>values.add(v),remove:v=>values.delete(v),contains:v=>values.has(v),toggle(v,force){const add=force===undefined?!values.has(v):force;add?values.add(v):values.delete(v);return add;}};
  }
  addEventListener(name,callback){this.listeners[name]=callback;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  removeAttribute(name){delete this.attributes[name];}
  getAttribute(name){return this.attributes[name];}
  querySelector(selector){if(selector==='span'){if(!this.span)this.span=new Element();return this.span;}throw new Error('Unexpected querySelector '+selector);}
  append(element){this.children.push(element);}
  remove(){}
  click(){if(this.listeners.click)this.listeners.click({target:this});}
  focus(){this.focused=true;}
  showModal(){this.attributes.open='';}
  close(){delete this.attributes.open;}
}

function makeSummary(level,key,period,total,citations,extra={}) {
  return {level,group_key:key,group_name:key,period,retrieval_status:'complete',period_assignment_status:period==='all'?'all_publications':'complete',faculty_count:level==='department_union'?2:1,faculty_searches_complete:level==='department_union'?2:1,publication_total:total,retrieved_publications_in_scope:total,current_citations_total:citations,known_citations_sum_on_retrieved_publications:citations??0,publications_missing_citations:citations===null?1:0,q1_publications_supplied_categories:total,q2_publications_supplied_categories:0,observed_q1_publications:total,observed_q2_publications:0,publications_without_supplied_quartile:0,unassigned_publications_potentially_in_scope:0,...extra};
}
const faculty=[
  {faculty_key:'synthetic:A',name:'Faculty A',programme:'Programme A',orcid:'synthetic-orcid-A',researcher_id:'synthetic-RID-A'},
  {faculty_key:'synthetic:B',name:'Faculty B',programme:'Programme B',orcid:'synthetic-orcid-B',researcher_id:'synthetic-RID-B'}
];
const journals=[{journal_key:'synthetic:J',journal_name:'Journal A',issn:'0000-0000',eissn:'0000-0000',jcr_year:'2025',category:'Synthetic category',jif:'<0.1',jif_rank:1,category_journals:4,derived_jif_quartile:'Q1'}];
function completeState() {
  const summary_rows=[];
  for(const period of ['all',2025]) {
    // Two faculty share one paper. The union row is 1, not the faculty sum 2.
    summary_rows.push(makeSummary('department_union','all_supplied_faculty',period,1,0));
    summary_rows.push(makeSummary('programme','Programme A',period,1,0));
    summary_rows.push(makeSummary('programme','Programme B',period,1,0));
    summary_rows.push(makeSummary('faculty','synthetic:A',period,1,0));
    summary_rows.push(makeSummary('faculty','synthetic:B',period,1,0));
  }
  return {faculty,journals,storage_path:'/synthetic/DEH_Research_Data.json',snapshot:{collected_at:'2026-11-21T12:00:00Z',run_status:'complete',version:1,publications:[{uid:'synthetic:P',title:'Shared synthetic paper',journal_name:'Journal A',doi:'10.synthetic/example',academic_year_start:2025,academic_year:'2025–2026',assignment_year:2025,publish_year_raw:2025,publish_month_raw:'AUG',current_wos_citations:0,citation_status:'available',best_supplied_quartile:'Q1',jcr_data_year:'2025',journal_match_status:'matched',jcr_category_records:'[]',date_status:'month',wos_url:'https://example.test/paper',last_retrieved_at:'2026-11-21T12:00:00Z'}],associations:{'synthetic:A':['synthetic:P'],'synthetic:B':['synthetic:P']},states:{'synthetic:A':{status:'complete'},'synthetic:B':{status:'complete'}},summary_rows,years:[2025],jcr_year:'2025'},job:{running:false,status:'complete',done:2,total:2,message:'Synthetic collection complete'}};
}
function environment(bootstrap,fetchHandler) {
  const elements=new Map();
  for(const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const tag=match[0], classes=(tag.match(/\bclass="([^"]*)"/)||[])[1]||'';
    const element=new Element(match[1],classes);
    const dataTab=(tag.match(/\bdata-tab="([^"]*)"/)||[])[1];
    if(dataTab)element.dataset.tab=dataTab;
    elements.set(match[1],element);
  }
  elements.get('deh-bootstrap').textContent=JSON.stringify(bootstrap);
  const navTabs=[...elements.values()].filter(e=>e.dataset.tab), nav=new Element(), documentListeners=new Map();
  const document={getElementById:id=>{if(!elements.has(id))throw new Error('Missing element '+id);return elements.get(id);},querySelector:selector=>{assert.equal(selector,'.nav');return nav;},querySelectorAll:selector=>{assert.equal(selector,'.nav-tab');return navTabs;},addEventListener(name,callback){if(!documentListeners.has(name))documentListeners.set(name,[]);documentListeners.get(name).push(callback);},createElement:()=>new Element(),body:new Element(),activeElement:null};
  const timers=new Map();let nextTimer=1;const requests=[];
  const context=vm.createContext({document,window:{print(){}},URL,Date,Map,Set,Number,String,Boolean,JSON,console,setTimeout(fn){const id=nextTimer++;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},fetch:async(url,options)=>{requests.push({url,options});if(!fetchHandler)throw new Error('Offline UI attempted fetch');return fetchHandler(url,options);}});
  vm.runInContext(executable,context,{filename:'ui.html'});
  return {context,elements,requests,timers,change(id,value){const element=elements.get(id);element.value=value;return element.listeners.change({target:element});},input(id,value){const element=elements.get(id);element.value=value;return element.listeners.input({target:element});},clickRendered(id,attribute,value){
    const tag=[...elements.get(id).innerHTML.matchAll(/<[^>]+>/g)].map(m=>m[0]).find(tag=>tag.includes(`${attribute}="${value}"`));
    assert(tag,`No rendered ${attribute}="${value}" control in ${id}`);
    const attributes=Object.fromEntries([...tag.matchAll(/([\w-]+)="([^"]*)"/g)].map(m=>[m[1],m[2]]));
    const dataset=Object.fromEntries(Object.entries(attributes).filter(([name])=>name.startsWith('data-')).map(([name,value])=>[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),value]));
    const target={dataset,disabled:/\sdisabled(?:\s|=|>)/.test(tag),closest(selector){const match=selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);return match&&Object.hasOwn(attributes,match[1])&&(match[2]===undefined||match[2]===attributes[match[1]])?this:null;}};
    for(const callback of documentListeners.get('click')||[])callback({target,preventDefault(){}});
  }};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const response=value=>({ok:true,status:200,json:async()=>value});

(async()=>{
  // Offline first view has no fetch or active refresh, and missing is not zero.
  const empty=environment({mode:'offline',state:{faculty,journals,snapshot:null,job:{running:false,status:'idle'}}});
  assert.equal(empty.requests.length,0);
  assert(empty.elements.get('live-refresh').classList.contains('hidden'));
  assert(!empty.elements.get('offline-refresh').classList.contains('hidden'));
  assert.equal((empty.elements.get('metric-cards').innerHTML.match(/Awaiting data/g)||[]).length,6);

  // Valid zero survives, while the programme/whole roster use keyed union summaries.
  const complete=environment({mode:'offline',state:completeState()});
  assert.equal(complete.context.__test.selectedSummary('all').publication_total,1);
  assert.equal(complete.context.__test.selectedSummary(2025).current_citations_total,0);
  assert.match(complete.elements.get('metric-cards').innerHTML,/class="metric-value ">0</);
  complete.change('programme','Programme B');
  assert.equal(complete.context.__test.selectedSummary(2025).group_key,'Programme B');
  assert.match(complete.elements.get('faculty-comparison').innerHTML,/Faculty B/);
  assert.doesNotMatch(complete.elements.get('faculty-comparison').innerHTML,/Faculty A/);
  assert.equal(complete.context.__test.publicationRows().length,1);

  // A faculty filter isolates an author's records; clearing restores the unique-paper union.
  const drillState=completeState();
  drillState.snapshot.publications.push({...drillState.snapshot.publications[0],uid:'synthetic:A-only',title:'Exclusive synthetic paper for Faculty A',current_wos_citations:3,best_supplied_quartile:'Q2'});
  drillState.snapshot.associations['synthetic:A'].push('synthetic:A-only');
  for(const row of drillState.snapshot.summary_rows) {
    const onlyB=['synthetic:B','Programme B'].includes(row.group_key), count=onlyB?1:2;
    Object.assign(row,{publication_total:count,retrieved_publications_in_scope:count,current_citations_total:onlyB?0:3,known_citations_sum_on_retrieved_publications:onlyB?0:3,q1_publications_supplied_categories:1,q2_publications_supplied_categories:count-1,observed_q1_publications:1,observed_q2_publications:count-1});
  }
  const drill=environment({mode:'offline',state:drillState});
  drill.change('faculty-filter','synthetic:B');
  assert.equal(drill.context.__test.selectedSummary(2025).level,'faculty');
  assert.equal(drill.context.__test.selectedSummary(2025).group_key,'synthetic:B');
  assert.equal(drill.context.__test.selectedSummary(2025).current_citations_total,0);
  assert.equal(JSON.stringify(drill.context.__test.publicationRows().map(p=>p.uid)),JSON.stringify(['synthetic:P']));
  assert.doesNotMatch(drill.elements.get('faculty-comparison').innerHTML,/Faculty A/);
  drill.change('programme','Programme A');
  assert.equal(drill.elements.get('faculty-filter').value,'');
  assert.equal(drill.context.__test.selectedSummary(2025).level,'programme');
  assert.equal(drill.context.__test.publicationRows().length,2);
  drill.elements.get('clear-filters').click();
  assert.equal(drill.elements.get('programme').value,'');
  assert.equal(drill.elements.get('faculty-filter').value,'');
  assert.equal(drill.context.__test.selectedSummary(2025).level,'department_union');
  assert.equal(drill.context.__test.selectedSummary(2025).publication_total,2);

  // Q1/Q2 cards open the matching publications. Selecting a faculty clears an old quartile.
  drill.clickRendered('metric-cards','data-open-quartile','Q2');
  assert.equal(drill.elements.get('publication-quartile').value,'Q2');
  assert.equal(drill.elements.get('publication-period').value,'selected');
  assert(!drill.elements.get('view-publications').classList.contains('hidden'));
  assert.equal(JSON.stringify(drill.context.__test.publicationRows().map(p=>p.uid)),JSON.stringify(['synthetic:A-only']));
  drill.context.__test.changeTab('overview');
  drill.clickRendered('faculty-comparison','data-faculty-key','synthetic:B');
  assert.equal(drill.elements.get('faculty-filter').value,'synthetic:B');
  assert.equal(drill.elements.get('publication-quartile').value,'');
  assert(!drill.elements.get('view-publications').classList.contains('hidden'));
  assert.equal(JSON.stringify(drill.context.__test.publicationRows().map(p=>p.uid)),JSON.stringify(['synthetic:P']));
  drill.context.__test.changeTab('overview');
  drill.clickRendered('metric-cards','data-open-quartile','Q1');
  assert.equal(drill.elements.get('publication-quartile').value,'Q1');
  assert.equal(JSON.stringify(drill.context.__test.publicationRows().map(p=>p.uid)),JSON.stringify(['synthetic:P']));
  assert.equal(drill.requests.length,0);

  // A long history must retain an older selected year, and never plot missing citations as zero.
  const history=completeState(), historyYears=Array.from({length:12},(_,i)=>2010+i);
  const historyGroups=[['department_union','all_supplied_faculty'],['programme','Programme A'],['programme','Programme B'],['faculty','synthetic:A'],['faculty','synthetic:B']];
  history.snapshot.publications=historyYears.map(y=>({...history.snapshot.publications[0],uid:`synthetic:year:${y}`,academic_year_start:y,academic_year:`${y}–${y+1}`,assignment_year:y,current_wos_citations:1}));
  history.snapshot.associations=Object.fromEntries(faculty.map(f=>[f.faculty_key,history.snapshot.publications.map(p=>p.uid)]));
  history.snapshot.years=historyYears;
  history.snapshot.requested_academic_year=2010;
  history.snapshot.summary_rows=['all',...historyYears].flatMap(period=>historyGroups.map(([level,key])=>makeSummary(level,key,period,period==='all'?12:1,period==='all'?12:1)));
  const timeline=environment({mode:'offline',state:history});
  assert.match(timeline.elements.get('year-chart').innerHTML,/2010–2011: 1 publications/);
  history.snapshot.publications[0].current_wos_citations=null;
  history.snapshot.publications[0].citation_status='unavailable';
  for(const row of history.snapshot.summary_rows.filter(r=>r.period==='all'||r.period===2010))Object.assign(row,{current_citations_total:null,publications_missing_citations:1,known_citations_sum_on_retrieved_publications:row.period==='all'?11:0});
  timeline.context.__test.setState(history);
  timeline.elements.get('chart-citations').click();
  assert.match(timeline.elements.get('year-chart').innerHTML,/selected[^<]*unavailable/i);
  assert.doesNotMatch(timeline.elements.get('year-chart').innerHTML,/selected academic year is highlighted/i);
  assert.doesNotMatch(timeline.elements.get('year-chart').innerHTML,/2010–2011: 0 current citations/);
  assert.equal(timeline.requests.length,0);

  // Missing period is not called a failed/complete search and does not show zero.
  complete.change('academic-year','2030');
  assert.match(complete.elements.get('overview-status').innerHTML,/not included in the saved report/);
  assert.doesNotMatch(complete.elements.get('overview-status').innerHTML,/collection is incomplete/);
  assert.match(complete.elements.get('faculty-comparison').innerHTML,/Year unavailable/);
  assert.match(complete.elements.get('publication-warning').innerHTML,/No summary is stored/);

  // Publication totals can be available while citation totals remain null.
  const missing=completeState();
  for(const row of missing.snapshot.summary_rows){row.current_citations_total=null;row.publications_missing_citations=1;}
  complete.context.__test.setState(missing);
  complete.change('programme','');complete.change('academic-year','2025');
  assert.equal(complete.context.__test.selectedSummary(2025).current_citations_total,null);
  assert.equal((complete.elements.get('metric-cards').innerHTML.match(/>Unavailable</g)||[]).length,2);

  // Data is escaped. Unsafe record URLs are not converted to clickable links.
  assert.equal(complete.context.__test.httpsLink('javascript:alert(1)','record'),'record');
  assert.equal(complete.context.__test.httpsLink('http://example.test','record'),'record');
  assert.match(complete.context.__test.doiLink('10.test/x#y?z'),/https:\/\/doi\.org\/10\.test\/x%23y%3Fz/);
  const untrusted=completeState();
  untrusted.faculty=[{...faculty[0],name:'<img src=x onerror=alert(1)>'},faculty[1]];
  untrusted.snapshot.publications[0].title='<script>alert(1)</script>';
  untrusted.snapshot.publications[0].wos_url='javascript:alert(1)';
  complete.context.__test.setState(untrusted);
  assert.match(complete.elements.get('faculty-comparison').innerHTML,/&lt;img/);
  assert.doesNotMatch(complete.elements.get('faculty-comparison').innerHTML,/<img/);
  assert.match(complete.elements.get('publication-table').innerHTML,/&lt;script&gt;/);
  complete.context.__test.showPublication('synthetic:P');
  assert.doesNotMatch(complete.elements.get('publication-dialog-body').innerHTML,/href="javascript:/);

  // A real refresh event sends the key only in the POST, clears it, polls and loads state.
  let serverState={faculty,journals,snapshot:null,job:{running:false,status:'idle'}};
  const live=environment({mode:'live',token:'synthetic-nonce'},async(url,options)=>{
    assert.equal(options.headers['X-DEH-Token'],'synthetic-nonce');
    if(url==='/api/state')return response(serverState);
    if(url==='/api/refresh'){
      assert.equal(options.method,'POST');
      assert.deepEqual(JSON.parse(options.body),{api_key:'synthetic-key',academic_year_start:2025});
      serverState={...serverState,job:{running:true,status:'running',done:0,total:2,faculty_name:'Faculty A'}};
      return {...response({accepted:true}),status:202};
    }
    if(url==='/api/status')return response({job:serverState.job,snapshot_version:serverState.snapshot?.version??0});
    throw new Error('Unexpected endpoint '+url);
  });
  await flush();await flush();
  assert.match(live.elements.get('footer-status').innerHTML,/Ready to collect/);
  live.elements.get('api-key').value='synthetic-key';
  await live.elements.get('refresh-form').listeners.submit({preventDefault(){}});
  assert.equal(live.elements.get('api-key').value,'');
  assert(live.elements.get('refresh-button').disabled);
  assert.equal(live.requests.filter(r=>r.url==='/api/refresh').length,1);
  assert(live.requests.filter(r=>r.url!=='/api/refresh').every(r=>!String(r.options.body||'').includes('synthetic-key')));
  serverState=completeState();
  await live.context.__test.pollStatus();
  assert.equal(live.context.__test.selectedSummary(2025).publication_total,1);
  assert(!live.elements.get('refresh-button').disabled);
  assert.match(live.elements.get('footer-status').innerHTML,/Data saved on this Mac/);
  live.context.__test.setState({...serverState,storage_warning:'Synthetic disk write failed.'});
  assert.match(live.elements.get('footer-status').innerHTML,/Results not saved/);
  assert.match(live.elements.get('storage-warning').innerHTML,/Synthetic disk write failed/);
  // GitHub reports open on the requested year and explain the actual refresh flow.
  const githubState=completeState();
  githubState.snapshot.requested_academic_year=2026;
  githubState.refresh_context={method:'github_actions',url:'https://github.com/example/deh/actions'};
  const github=environment({mode:'offline',state:githubState});
  assert.equal(github.elements.get('academic-year').value,'2026');
  assert.match(github.elements.get('offline-refresh-copy').innerHTML,/Refresh research data/);
  assert.match(github.elements.get('offline-refresh-copy').innerHTML,/https:\/\/github.com\/example\/deh\/actions/);
  assert.match(github.elements.get('help-step3-copy').textContent,/DEH_Research_Report.html/);
  assert.match(github.elements.get('overview-status').innerHTML,/GitHub/);
  assert.equal(github.requests.length,0);
  assert.equal(complete.requests.length,0);
  const preview=environment({mode:'offline',state:{faculty,journals,snapshot:null,job:{running:false,status:'idle'},refresh_context:{method:'github_actions',url:'https://github.com/example/deh/actions',default_academic_year:2026}}});
  assert.equal(preview.elements.get('academic-year').value,'2026');
  assert.doesNotMatch(preview.elements.get('overview-status').innerHTML,/Enter your API key/);
  assert.equal((preview.elements.get('metric-cards').innerHTML.match(/Awaiting data/g)||[]).length,6);
  assert.equal(preview.requests.length,0);
  console.log('Frontend smoke checks passed: refresh event, period/programme/faculty union selection, metric and faculty drill-downs, null versus zero, escaping, offline preview, and persistence status.');
})().catch(error=>{console.error(error);process.exitCode=1;});
