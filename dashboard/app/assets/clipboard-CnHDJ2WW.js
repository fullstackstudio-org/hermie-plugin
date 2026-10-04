const t=()=>typeof navigator>"u"?null:navigator;async function e(r,a=t()){if(!r||!a?.clipboard)return!1;try{return await a.clipboard.writeText(r),!0}catch{return!1}}export{e as w};
