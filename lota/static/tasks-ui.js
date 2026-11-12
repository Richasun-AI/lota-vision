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
function annotationTaskEditor(){
 const project=state.project,config=structuredClone(project.config),definitions=config.annotation_tasks||{};
 const choices=select([["","新建任务"],...Object.entries(definitions).map(([id,t])=>[id,t.name])],"");
 const id=textInput(),name=textInput(),task=select([["detect","检测：画框"],["classify","分类：整图单选"]],"detect");
 const classes=lines(),site=h("input",{type:"checkbox",checked:false});
 const url=textInput(),studioId=textInput(),env=textInput("LOTA_LABEL_STUDIO_TOKEN"),files=textInput();
 choices.onchange=()=>{const t=definitions[choices.value];id.value=choices.value;id.readOnly=!!t;name.value=t?.name||"";
 task.value=t?.task||"detect";classes.value=(t?.classes||[]).join("\n");site.checked=t?.require_site===true;
 url.value=t?.label_studio?.base_url||"";studioId.value=t?.label_studio?.project_id||"";files.value=t?.label_studio?.local_files_root||"";};
 const form=formContent(h("div",{},choices,h("p",{},"分类每图只选一个标签；检测需要标全当前任务类别，并确认无目标样本。"),
 h("div",{class:"form-grid"},field("任务代码",id),field("任务名称",name),field("任务类型",task),
 field("标签代码，每行一个",classes,"同一场景可建立多套任务",true),field("按站点划分数据",site),
 field("Label Studio 地址",url),field("Label Studio 项目 ID",studioId),field("令牌环境变量",env),
 field("本地图片服务根目录",files))),"保存任务",async()=>{
  const taskId=id.value.trim();if(!taskId||!name.value.trim())throw Error("请填写任务代码和名称");
  const value={name:name.value.trim(),task:task.value,classes:splitLines(classes.value),require_site:site.checked};
  if(url.value.trim())value.label_studio={base_url:url.value.trim(),project_id:Number(studioId.value),token_env:env.value.trim(),local_files_root:files.value.trim()};
  config.annotation_tasks={...definitions,[taskId]:value};
  await request(pathFor(project),"PUT",{config,expected_revision:project.revision});
  state.annotationTask=taskId;dialog.close();await initialize(project.id);
 });
 openDialog("标注任务管理",form,true);
}
