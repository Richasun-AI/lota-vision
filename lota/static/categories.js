function categoryLabel(code,config=state.project?.config){
 const item=config?.categories?.[code];
 return item?(item.name+(item.name_en?" / "+item.name_en:"")+(item.enabled?"":" [停用]")):code+" [待命名]";
}
async function categoryEditor(){
 const project=state.project,config=structuredClone(project.config);
 const seen=await request(pathFor(project)+"/sorting?limit=1");
 const definitions=config.categories||{};
 const codes=[...new Set([...Object.keys(definitions),...config.classes,...Object.keys(seen.categories)])];
 const body=h("div",{}),rows=[];
 const search=textInput("",{placeholder:"搜索代码或名称"});
 const list=h("div",{style:"max-height:60vh;overflow:auto"});
 function add(code="",definition={},existing=false){
  const controls={code:textInput(code,{readOnly:existing}),name:textInput(definition.name||""),
   english:textInput(definition.name_en||""),description:textInput(definition.description||""),
   enabled:h("input",{type:"checkbox",checked:definition.enabled!==false})};
  const inModel=existing&&config.classes.includes(code),photos=existing?(seen.categories[code]||0):0;
  const entry={controls};
  const remove=button("删除",()=>{
   if(inModel){UI.toast("“"+code+"”是当前模型训练类别，不能删除；不想用可取消“启用”",true);return;}
   if(photos){UI.toast("“"+code+"”已有 "+photos+" 张分拣照片，不能删除；不想用可取消“启用”",true);return;}
   if(existing&&!confirm("删除类别“"+(controls.name.value||code)+"”？点“保存类别”后生效"))return;
   rows.splice(rows.indexOf(entry),1);row.remove();
  });
  const row=h("div",{class:"panel category-row"},h("div",{class:"form-grid"},
   field("稳定代码",controls.code),field("中文名称",controls.name),field("英文名称",controls.english),
   field("说明",controls.description),field("启用",controls.enabled)),
   h("div",{class:"actions"},h("small",{},inModel?"当前模型训练类别":"照片分拣类别"+(photos?" · "+photos+" 张":"")),remove));
  entry.row=row;rows.push(entry);list.append(row);
 }
 codes.forEach(code=>add(code,definitions[code]||{},true));
 search.oninput=()=>rows.forEach(({controls,row})=>row.hidden=![controls.code.value,controls.name.value,controls.english.value].join(" ").toLowerCase().includes(search.value.toLowerCase()));
 body.append(h("p",{},"类别属于当前子项目场景。已有代码保持稳定，修改名称会更新图库显示；停用类别仍保留历史记录。没用过的类别可以删除；模型训练类别和已有分拣照片的类别只能停用。"),
 search,h("div",{class:"actions"},button("新增类别",()=>add()),
 button("导入标准照片类型（中英）",asyncClick(async()=>{
  const std=await fetch("/static/standard_categories.json").then(r=>r.ok?r.json():{}).catch(()=>({}));if(!Object.keys(std).length){UI.toast("没有标准照片类型清单（static/standard_categories.json）",true);return;}
  const have=new Set(rows.map(r=>r.controls.code.value.trim()));let n=0;
  for(const [code,d] of Object.entries(std)){
   const old=rows.find(r=>r.controls.code.value.trim()===code);
   if(old){if(!old.controls.name.value.trim()){old.controls.name.value=d.name;old.controls.english.value=d.name_en;old.controls.description.value=d.description;n++;}continue;}
   if(!have.has(code)){add(code,d);n++;}
  }
  notifyUser("已加入 "+n+" 个类别，点“保存类别”生效");
 }),"primary")),list);
 openDialog(project.config.name+" · 类别编辑器",formContent(body,"保存类别",async()=>{
  const categories={};
  for(const {controls:c} of rows){
   const code=c.code.value.trim(),name=c.name.value.trim();
   if(!code||!name)throw Error("每个类别都需要代码和名称；请为待命名类别补充名称");
   if(!/^[a-z][a-z0-9_.-]{0,63}$/.test(code))throw Error("代码使用小写字母、数字、下划线或短横线");
   if(categories[code])throw Error("类别代码重复："+code);
   categories[code]={name,name_en:c.english.value.trim(),description:c.description.value.trim(),enabled:c.enabled.checked};
  }
  config.categories=categories;
  await request(pathFor(project),"PUT",{config,expected_revision:project.revision});
  dialog.close();await initialize(project.id);notifyUser("类别已保存");
 }),true);
}
