async (page) => {
  await page.getByRole('link',{name:/^Loop/}).click();
  const row = page.getByRole('main').getByText('周六安排个游泳',{exact:true});
  await row.waitFor();
  return {row: await row.evaluate(el => {const s=getComputedStyle(el);return {fontSize:s.fontSize,fontFamily:s.fontFamily,lineHeight:s.lineHeight,style:el.getAttribute('style')};}),
    title: await page.getByRole('heading',{name:'Loop',exact:true}).evaluate(el=>({fontSize:getComputedStyle(el).fontSize})),
    metadata: await row.evaluate(el=>{const parent=el.parentElement;return [...parent.querySelectorAll('span')].map(n=>({text:n.textContent,fontSize:getComputedStyle(n).fontSize,fontFamily:getComputedStyle(n).fontFamily}));})};
}
