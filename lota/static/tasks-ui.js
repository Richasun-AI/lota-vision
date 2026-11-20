function annotationTaskPicker(){
 const definitions=state.project.config.annotation_tasks||{};
 const picker=select([["","历史默认任务"],...Object.entries(definitions).map(([id,t])=>[id,t.name+" · "+t.task])],state.annotationTask||"");
 picker.onchange=()=>{state.annotationTask=picker.value;navigate(state.tab);};
 return panel(field("当前标注任务",picker),button("管理标注任务",()=>annotationTaskEditor()),button("\u67e5\u770b\u6807\u6ce8\u754c\u9762\u914d\u7f6e",asyncClick(async()=>{
 if(!state.annotationTask)throw Error("Select an annotation task first");
 const data=await request(pathFor()+"/annotation-tasks/"+state.annotationTask+"/config");
 openDialog(data.task.name,code(data.label_config),true);
 })));
}
// 标签选择器：从本场景的类别（类别编辑器）和照片类型页的图库里勾选，不用手写代码；另可补充“其他代码”
const LABEL_CODE=/^[a-z0-9][a-z0-9_.-]{0,63}$/, TASK_CODE=/^[a-z0-9][a-z0-9-]{0,62}$/;
function labelPicker(config){
 const items=new Map(),checked=new Set();let order=[];
 const put=(code,info,src)=>{const it=items.get(code)||{code,name:"",name_en:"",src:new Set(),enabled:true};
  it.name=it.name||info.name||"";it.name_en=it.name_en||info.name_en||"";if(info.enabled===false)it.enabled=false;it.src.add(src);items.set(code,it);};
 for(const [code,c] of Object.entries(config.categories||{}))put(code,c,(config.classes||[]).includes(code)?"训练类别":"分拣类别");
 for(const code of config.classes||[])put(code,{},"训练类别");
 const search=textInput("",{placeholder:"搜索代码、中文或英文名"}),count=h("span",{class:"small muted"}),extra=textInput("",{placeholder:"其他代码，逗号分隔（列表里没有的才填）"});
 const list=h("div",{style:"max-height:42vh;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:2px 12px;padding:6px;border:1px solid var(--line,#ddd);border-radius:6px"});
 const libs=h("span");
 const visible=()=>[...items.values()].filter(it=>!list.dataset.q||[it.code,it.name,it.name_en].join(" ").toLowerCase().includes(list.dataset.q));
 function render(){
  const rows=visible().sort((a,b)=>(checked.has(b.code)-checked.has(a.code))||a.code.localeCompare(b.code));
  list.replaceChildren(...rows.map(it=>{
   const box=h("input",{type:"checkbox",checked:checked.has(it.code)});
   box.onchange=()=>{box.checked?checked.add(it.code):checked.delete(it.code);count.textContent="已选 "+checked.size+" 个";};
   return h("label",{style:"display:flex;gap:6px;align-items:baseline;padding:2px 0;cursor:pointer"+(it.enabled?"":";opacity:.55"),title:[...it.src].join("、")},box,
    h("span",{},h("code",{},it.code),h("span",{class:"small"}," "+[it.name,it.name_en].filter(Boolean).join(" / ")+(it.enabled?"":" [停用]")),
     h("span",{class:"small muted"}," · "+[...it.src].join("、"))));
  }));
  count.textContent="已选 "+checked.size+" 个";
 }
 search.onkeydown=extra.onkeydown=(e)=>{if(e.key==="Enter")e.preventDefault();};
 search.oninput=()=>{list.dataset.q=search.value.trim().toLowerCase();render();};
 const pick=(fn)=>()=>{for(const it of visible())fn(it)?checked.add(it.code):null;render();};
 const toolbar=h("div",{class:"actions"},search,
  button("全选",pick(()=>true),"small"),button("全不选",()=>{for(const it of visible())checked.delete(it.code);render();},"small"),
  button("选照片类型",pick(it=>it.src.has("照片类型")&&it.enabled&&it.code!=="other"),"small",{title:"只勾照片类型页图库里的类型（不含临时仓库 other、unknown、检测设备类别）"}),
  button("选训练类别",pick(it=>it.src.has("训练类别")),"small"),libs,count);
 // 照片类型页的图库：取类型清单（中英文名）一起列出来
 (async()=>{
  const ls=await request("/api/types/libs").catch(()=>({libs:[]}));if(!ls.libs?.length)return;
  const load=async(lib)=>{const t=await request("/api/types/taxonomy?lib="+encodeURIComponent(lib||""));
   for(const it of [...items.values()]){it.src.delete("照片类型");if(!it.src.size&&!checked.has(it.code))items.delete(it.code);}
   for(const [code,v] of Object.entries(t.types||{}))if(LABEL_CODE.test(code))put(code,v,"照片类型");render();};
  if(ls.libs.length>1){const s=select(ls.libs.map(l=>[l.id,"图库："+l.name]),ls.default);s.onchange=()=>load(s.value).catch(e=>notifyUser(e.message,true));libs.append(s);}
  await load(ls.default);
 })().catch(()=>{});
 return {
  el:h("div",{},toolbar,list,extra),
  set(values){checked.clear();order=[...values];for(const v of values){if(!items.has(v))put(v,{},"已在任务里");checked.add(v);}extra.value="";render();},
  values(){
   const more=extra.value.split(/[\n,，]+/).map(x=>x.trim()).filter(Boolean);
   const picked=[...order.filter(v=>checked.has(v)),...[...checked].filter(v=>!order.includes(v)).sort()];
   return [...new Set([...picked,...more])];
  },
 };
}
function annotationTaskEditor(){
 const project=state.project,config=structuredClone(project.config),definitions=config.annotation_tasks||{};
 const choices=select([["","新建任务"],...Object.entries(definitions).map(([id,t])=>[id,t.name])],"");
 const id=textInput("",{placeholder:"如 telecom-site-inspection"}),name=textInput(),task=select([["detect","检测：画框"],["classify","分类：整图单选"]],"detect");
 const classes=labelPicker(config),site=h("input",{type:"checkbox",checked:false});
 const url=textInput(),studioId=textInput(),env=textInput("LOTA_LABEL_STUDIO_TOKEN"),files=textInput();
 choices.onchange=()=>{const t=definitions[choices.value];id.value=choices.value;id.readOnly=!!t;name.value=t?.name||"";
 task.value=t?.task||"detect";classes.set(t?.classes||[]);site.checked=t?.require_site===true;
 url.value=t?.label_studio?.base_url||"";studioId.value=t?.label_studio?.project_id||"";files.value=t?.label_studio?.local_files_root||"";};
 classes.set([]);
 const form=formContent(h("div",{},choices,h("p",{},"分类每图只选一个标签；检测需要标全当前任务类别，并确认无目标样本。"),
 h("div",{class:"form-grid"},field("任务代码",id,"小写字母、数字、短横线，如 telecom-site-inspection；建好后不能改"),field("任务名称",name),field("任务类型",task),
 field("标签",classes.el,"从本场景的类别和照片类型里勾选；中英文名只用来看，保存的是代码",true),field("按站点划分数据",site),
 field("Label Studio 地址",url),field("Label Studio 项目 ID",studioId),field("令牌环境变量",env),
 field("本地图片服务根目录",files))),"保存任务",async()=>{
  const taskId=id.value.trim();if(!taskId||!name.value.trim())throw Error("请填写任务代码和名称");
  if(!definitions[taskId]&&!TASK_CODE.test(taskId))throw Error("任务代码“"+taskId+"”不合规：只能用小写字母、数字、短横线（不能有大写、下划线、空格），如 "+(taskId.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"")||"telecom-site-inspection"));
  const labels=classes.values();if(!labels.length)throw Error("至少勾选一个标签");
  const bad=labels.filter(v=>!LABEL_CODE.test(v));
  if(bad.length)throw Error("这些不是合规的标签代码："+bad.join("、")+"。代码只能用小写字母、数字和 _ . -；中英文名不用填，勾选列表里对应的那一项即可");
  const value={name:name.value.trim(),task:task.value,classes:labels,require_site:site.checked};
  if(url.value.trim())value.label_studio={base_url:url.value.trim(),project_id:Number(studioId.value),token_env:env.value.trim(),local_files_root:files.value.trim()};
  config.annotation_tasks={...definitions,[taskId]:value};
  await request(pathFor(project),"PUT",{config,expected_revision:project.revision});
  state.annotationTask=taskId;dialog.close();await initialize(project.id);
 });
 openDialog("标注任务管理",form,true);
}
