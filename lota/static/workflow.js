
async function legacyWorkflowsPage(){  // 旧编辑器（已迁移到 /flow，保留代码备查）
 const modelData=await request(pathFor()+"/models");
 const saved=await request(pathFor()+"/workflows"),name=textInput("自动分拣"),key=textInput("automatic-sorting");
 const weightFiles=(await request("/api/weights").catch(()=>({weights:[]}))).weights||[];
 let nodes=[],from=null;
 const board=h("div",{class:"node-stage"}),svg=document.createElementNS("http://www.w3.org/2000/svg","svg"),settings=h("div",{class:"node-inspector"});
 svg.classList.add("node-wires");board.append(svg);
 function linesDraw(){svg.replaceChildren();nodes.forEach(n=>{const ref=n.args.image_ids?.$ref;if(!ref)return;const p=nodes.find(x=>ref.startsWith(x.id+"."));if(!p)return;
 const line=document.createElementNS(svg.namespaceURI,"path");
 line.setAttribute("d","M"+(p.x+230)+","+(p.y+50)+" C"+(p.x+290)+","+(p.y+50)+" "+(n.x-70)+","+(n.y+50)+" "+n.x+","+(n.y+50));svg.append(line);});}
 function inspect(n){
  settings.replaceChildren(h("h3",{},state.tools.find(t=>t.id===n.tool).title));
  const schema=state.tools.find(t=>t.id===n.tool).schema;
  Object.entries(schema.properties||{}).forEach(([k,s])=>{
   if(k==="annotation_task"){
    const input=select([["","Legacy default"],...Object.entries(state.project.config.annotation_tasks||{}).map(([id,t])=>[id,t.name+" / "+t.task])],n.args[k]||"");
    input.onchange=()=>{if(input.value)n.args[k]=input.value;else delete n.args[k];};
    settings.append(field("Annotation task",input));return;
   }
   if(k==="weights"){  // 权重：从服务器权重目录里选 .pt，不用手填路径
    const cur=n.args.weights&&weightFiles.includes(n.args.weights)?n.args.weights:(weightFiles[0]||"");
    n.args.weights=cur;
    const input=select(weightFiles.length?weightFiles.map(w=>[w,w.split("/").pop()]):[["","（服务器权重目录里没有 .pt）"]],cur);
    const info=h("small",{},"");
    const show=()=>request("/api/weights/info?path="+encodeURIComponent(input.value)).then(i=>info.textContent="能识别："+i.names.join("、")+"（"+(i.task==="classify"?"分类":"检测")+"模型）").catch(()=>info.textContent="");
    input.onchange=()=>{n.args.weights=input.value;show();};
    settings.append(field("权重",input,"用哪个 YOLO 权重来找图"),info);if(input.value)show();return;
   }
   if(k==="labels"&&n.tool==="curation.find_by_model"){
    const input=textInput(Array.isArray(n.args.labels)?n.args.labels.join(","):(n.args.labels||""));
    input.onchange=()=>n.args.labels=input.value.trim();
    settings.append(field("找什么目标",input,"类别名，多个用逗号隔开，如 weeds；留空 = 这个权重能认出的任何目标"));return;
   }
   if(k==="labels"){
    const aliases=modelData.aliases||{};
    const target=typeof aliases[n.args.selector]==="string"?aliases[n.args.selector]:aliases[n.args.selector]?.version||n.args.selector;
    const model=(modelData.models||[]).find(m=>m.version===target);
    const names=model?.classes||[];
    const supported=Array.isArray(names)?names:Object.values(names);
    if(supported.length){
     const box=h("div",{class:"target-choices"});
     supported.forEach(label=>{
      const input=h("input",{type:"checkbox",checked:(n.args.labels||[]).includes(label)});
      input.onchange=()=>{const chosen=new Set(n.args.labels||[]);input.checked?chosen.add(label):chosen.delete(label);n.args.labels=[...chosen];};
      box.append(h("label",{},input,label));
     });
     settings.append(field("\u68c0\u6d4b\u76ee\u6807\uff08\u53ef\u591a\u9009\uff09",box));
    }else{
     const input=textInput((n.args.labels||[]).join(","));
     input.onchange=()=>n.args.labels=splitLines(input.value);
     settings.append(field("\u76ee\u6807\u7c7b\u522b",input,"\u5148\u9009\u62e9\u5df2\u6ce8\u518c\u6a21\u578b\uff0c\u6216\u586b\u5199\u6a21\u578b\u7c7b\u522b\u4ee3\u7801"));
    }return;
   }
   if(k==="selector"&&n.tool==="selection.find"){
    const options=[...new Set(["general-detector",...(modelData.models||[]).map(m=>m.version),...Object.keys(modelData.aliases||{})])];
    const input=select(options.map(v=>[v,v]),n.args.selector||"general-detector");
    input.onchange=()=>{n.args.selector=input.value;n.args.labels=[];inspect(n);};
    settings.append(field("\u68c0\u6d4b\u6a21\u578b",input));return;
   }
   if(k==="image_ids"){settings.append(h("p",{},"图片输入通过节点端口连接。未连接时使用工作台选中图片。"));return;}
   const v=n.args[k]??s.default??"",input=s.type==="boolean"?h("input",{type:"checkbox",checked:!!v}):textInput(Array.isArray(v)?v.join(","):v);
   input.onchange=()=>{if(input.value===""&&s.type!=="boolean"){delete n.args[k];return;}
    n.args[k]=s.type==="boolean"?input.checked:s.type==="array"?splitLines(input.value):["integer","number"].includes(s.type)?Number(input.value):input.value;};
   settings.append(field(fieldLabels[k]||k,input,typeof helpFor==="function"?helpFor(n.tool,k):""));
  });
  settings.append(button("删除节点",()=>{nodes=nodes.filter(x=>x!==n);render();settings.replaceChildren();}));
 }
 function render(){board.querySelectorAll(".workflow-node").forEach(x=>x.remove());nodes.forEach(n=>{
  const title=h("div",{class:"node-title"},state.tools.find(t=>t.id===n.tool).title);
  const card=h("div",{class:"workflow-node",style:"left:"+n.x+"px;top:"+n.y+"px"},title,
   h("div",{class:"node-ports"},button("● 输入",()=>{if(from&&from!==n){
    if(!(state.tools.find(t=>t.id===n.tool).schema.properties||{}).image_ids){notifyUser("「"+state.tools.find(t=>t.id===n.tool).title+"」没有图片输入，不能接在别的节点后面",true);from=null;return;}
    n.args.image_ids={$ref:from.id+".image_ids"};from=null;linesDraw();}}),
   button("输出 ●",()=>{from=n;notifyUser("点击目标节点输入端口");})),h("small",{},n.id));
  card.onclick=()=>inspect(n);
  title.onpointerdown=e=>{e.preventDefault();title.setPointerCapture(e.pointerId);const x=e.clientX,y=e.clientY,ox=n.x,oy=n.y;
   title.onpointermove=p=>{n.x=Math.max(0,ox+p.clientX-x);n.y=Math.max(0,oy+p.clientY-y);card.style.left=n.x+"px";card.style.top=n.y+"px";linesDraw();};
   title.onpointerup=()=>title.onpointermove=null;};board.append(card);
 });linesDraw();}
 function add(tool,x,y){const n={id:"n"+Math.random().toString(16).slice(2,10),tool,x,y,args:{}};
 const props=state.tools.find(t=>t.id===tool).schema.properties||{};
 Object.entries(props).forEach(([k,s])=>{if(s.default!==undefined)n.args[k]=s.default;});
 if(props.image_ids)n.args.image_ids=[...selected()];nodes.push(n);render();inspect(n);return n;}
 function defaults(){nodes=[];let last;["catalog.scan","sorting.candidates","sorting.quality","sorting.duplicates","sorting.classify"].forEach((id,i)=>{
 const n=add(id,25+i*260,80+i%2*140);if(i>1)n.args.image_ids={$ref:last.id+".image_ids"};last=n;});render();}
 const palette=h("div",{class:"tool-palette"},...state.tools.map(t=>h("button",{type:"button",draggable:true,
 ondragstart:e=>e.dataTransfer.setData("text/plain",t.id),onclick:()=>add(t.id,40+nodes.length*25,50+nodes.length*20)},t.title)));
 board.ondragover=e=>e.preventDefault();board.ondrop=e=>{e.preventDefault();const id=e.dataTransfer.getData("text/plain"),r=board.getBoundingClientRect();
 if(state.tools.some(t=>t.id===id))add(id,e.clientX-r.left+board.scrollLeft,e.clientY-r.top+board.scrollTop);};
 async function save(){const ordered=[],done=new Set(),visiting=new Set();
 function visit(n){if(done.has(n.id))return;if(visiting.has(n.id))throw Error("连接形成循环");visiting.add(n.id);
 Object.values(n.args).forEach(v=>{if(v?.$ref){const p=nodes.find(x=>v.$ref.startsWith(x.id+"."));if(!p)throw Error("连接节点不存在");visit(p);}});
 visiting.delete(n.id);done.add(n.id);ordered.push({id:n.id,tool:n.tool,args:n.args,position:{x:n.x,y:n.y}});}
 nodes.forEach(visit);await request(pathFor()+"/workflows/"+key.value,"PUT",{name:name.value,steps:ordered});}
 defaults();
 return h("div",{},page("节点工作流","拖工具进入画布，拖动节点标题；先点击输出，再点另一节点输入完成连线。"),
 h("p",{class:"small"},"推荐用新版工作流编辑器（节点式，参数都有下拉选项和说明、带现成模板）：",h("a",{href:"/flow",target:"_blank"},"打开 /flow")),
 h("div",{class:"actions"},field("名称",name),field("ID",key),button("默认分拣流程",defaults),
 button("保存",asyncClick(async()=>{await save();notifyUser("已保存");})),
 button("保存并运行",asyncClick(async()=>{await save();await request(pathFor()+"/workflows/"+key.value+"/run","POST",{inputs:{}});navigate("jobs");}),"primary")),
 palette,h("div",{class:"node-editor"},board,settings),
 ...saved.map(w=>button("打开 "+w.definition.name,()=>{name.value=w.definition.name;key.value=w.id;nodes=w.definition.steps.map((s,i)=>({id:s.id,tool:s.tool,args:s.args,x:s.position?.x??i*260,y:s.position?.y??80}));render();})));
}

// 工作流已迁移到新版编辑器（/flow，节点式：模板、下拉选项、参数说明、续跑）
async function workflowsPage(){
 const pid=state.project?.id||"";
 const saved=pid?await request(pathFor()+"/workflows").catch(()=>[]):[];
 const go=(q)=>"/flow?"+new URLSearchParams(q).toString();
 return h("div",{},page("工作流","工作流已迁移到新版编辑器：带现成模板、每个参数都有下拉选项和说明、运行中能看进度、中断后能续跑。"),
  panel(h("a",{href:go({project:pid}),target:"_blank",class:"button primary",style:"display:inline-block;padding:8px 16px;text-decoration:none"},"⚡ 打开新版工作流编辑器")),
  saved.length?panel(h("strong",{},"本场景已保存的工作流（点击在新版里打开）"),
   ...saved.map(w=>h("div",{class:"rail"},h("span",{},w.id+" · "+(w.definition?.name||"")+" · "+(w.definition?.steps||[]).length+" 个节点"),
    h("a",{href:go({project:pid,workflow:w.id}),target:"_blank"},"在新版里打开 →")))):empty("本场景还没有保存的工作流。到新版编辑器里选一个模板开始。"));
}
