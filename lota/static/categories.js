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
 // 照片类型以“照片类型”页的图库为准（那里细化 / 改名 / 并入 / 删除）；没有图库时退回内置的 44 个标准类型
 const libs=await request("/api/types/libs").catch(()=>({libs:[]}));
 const libSel=libs.libs?.length>1?select(libs.libs.map(l=>[l.id,"图库："+l.name]),libs.default):null;
 const mark=(row,color)=>{row.style.borderLeft="4px solid "+color;};
 const syncTypes=asyncClick(async()=>{
  const std=await fetch("/static/standard_categories.json").then(r=>r.ok?r.json():{}).catch(()=>({}));
  let types=std,removed={},source="内置标准照片类型";
  if(libs.libs?.length){
   const t=await request("/api/types/taxonomy?lib="+encodeURIComponent(libSel?.value||""));
   types=t.types;removed=t.removed||{};source="图库“"+t.lib.name+"”";
  }
  if(!Object.keys(types).length){notifyUser("没有可同步的照片类型：先在“照片类型”页建图库",true);return;}
  const find=code=>rows.find(r=>r.controls.code.value.trim()===code);
  let added=0,renamed=0,disabled=0;const bad=[];
  for(const [code,d] of Object.entries(types)){
   if(!/^[a-z][a-z0-9_.-]{0,63}$/.test(code)){bad.push(code);continue;}
   const old=find(code),c=old?.controls;
   if(!old){add(code,{name:d.name,name_en:d.name_en,description:std[code]?.description||"",enabled:true});mark(rows.at(-1).row,"#2e7d32");added++;continue;}
   if(c.name.value.trim()!==d.name||(d.name_en&&c.english.value.trim()!==d.name_en)){
    c.name.value=d.name;if(d.name_en)c.english.value=d.name_en;
    if(!c.description.value.trim()&&std[code])c.description.value=std[code].description;
    mark(old.row,"#1565c0");renamed++;
   }
  }
  for(const [code,into] of Object.entries(removed)){
   const old=find(code);if(!old||!old.controls.enabled.checked)continue;
   old.controls.enabled.checked=false;
   if(into&&!old.controls.description.value.includes("已并入"))old.controls.description.value=(old.controls.description.value+" （已并入 "+into+"）").trim();
   mark(old.row,"#c62828");disabled++;
  }
  const parts=[added&&"新增 "+added,renamed&&"改名 "+renamed,disabled&&"停用 "+disabled+"（照片类型页已删除/并入）"].filter(Boolean);
  notifyUser(parts.length?"从"+source+"同步："+parts.join("，")+"。左边有色条的是变动行，点“保存类别”生效"
   +(bad.length?"；代号不合规跳过："+bad.join("、"):""):"已和"+source+"一致，没有要改的");
 });
 // 导出：按编辑器里当前看到的内容（含未保存的修改）导出 CSV（Excel 直接打开）或 JSON
 const exportCategories=fmt=>{
  const items=rows.map(({controls:c})=>{const code=c.code.value.trim();return{code,name:c.name.value.trim(),name_en:c.english.value.trim(),
   description:c.description.value.trim(),enabled:c.enabled.checked,model_class:config.classes.includes(code),photos:seen.categories[code]||0};}).filter(x=>x.code);
  const stamp=new Date().toISOString().slice(0,10),base="分拣类别_"+(config.name||project.id)+"_"+stamp;
  let text,mime;
  if(fmt==="json"){text=JSON.stringify({project:{id:project.id,name:config.name||""},exported_at:new Date().toISOString(),categories:items},null,2);mime="application/json";}
  else{
   const cell=v=>{v=v==null?"":String(v);return /[",\r\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;};
   text="﻿"+[["代码","中文名称","英文名称","说明","启用","训练类别","分拣照片数"],
    ...items.map(x=>[x.code,x.name,x.name_en,x.description,x.enabled?"是":"否",x.model_class?"是":"否",x.photos])].map(r=>r.map(cell).join(",")).join("\r\n");
   mime="text/csv";
  }
  const url=URL.createObjectURL(new Blob([text],{type:mime+";charset=utf-8"})),a=h("a",{href:url,download:base+"."+fmt});
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);
  notifyUser("已导出 "+items.length+" 个类别");
 };
 const exportPick=h("select",{class:"btn small",title:"导出编辑器里当前的类别（含未保存的修改）"},
  h("option",{value:""},"⇩ 导出"),h("option",{value:"csv"},"CSV（Excel）"),h("option",{value:"json"},"JSON"));
 exportPick.onchange=()=>{const f=exportPick.value;exportPick.value="";if(f)exportCategories(f);};
 body.append(h("p",{},"类别属于当前子项目场景。已有代码保持稳定，修改名称会更新图库显示；停用类别仍保留历史记录。没用过的类别可以删除；模型训练类别和已有分拣照片的类别只能停用。"),
 h("p",{class:"small muted"},"“从照片类型同步”以照片类型页的图库为准：新类型加进来，改过名的跟着改，删除或并入别类的在这里停用（不删，历史照片还在）。在照片类型页细化后，回来再点一次即可。"),
 search,h("div",{class:"actions"},button("新增类别",()=>add()),libSel||"",
 button("从照片类型同步（中英）",syncTypes,"primary"),exportPick),list);
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
