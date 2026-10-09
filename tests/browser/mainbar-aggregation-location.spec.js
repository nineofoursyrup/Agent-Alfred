import {test,expect} from '@playwright/test';
import {controlledTransport} from './transport.js';

for(const [reason,label] of [['sources_not_selected','未选择来源'],['no_matching_sources','本次有限查询/窗口无资料']])test(`exact aggregation NoAction keeps independent disposition and ${reason}`,async({page})=>{
  const session=await controlledTransport(page);
  await page.route('**/api/mainbar/locate?*',route=>route.fulfill({json:{
    process_instance_id:'test-process',session_id:session,run_id:'no-action',purpose:'aggregation',item_key:session+':no-action',source:'recorded_pair',user:{availability:'full',blocks:[{type:'text',text:'聚合请求'}],preview:null},reply_text:null,reply_disposition:'no_reply',recording_state:'recorded',recording_source:'persisted_record',history_contiguous:false,
    aggregation:{graph_result:'NoAction',reply_disposition:'no_reply',reason_code:reason,evidence_state:'known'},
  }}));
  await page.evaluate(async session=>{const {dashboard}=await import('/assets/app.js');await dashboard.locateReply({process_instance_id:'test-process',session_id:session,run_id:'no-action',action_id:'explicit-no-action'});},session);
  const article=page.locator('#messages [data-run-id="no-action"]');
  await expect(article).toContainText(label);
  await expect(article).not.toContainText('按要求未回复');
  await expect(article).toContainText('已保存');
  await expect(page.locator('#shell-status')).not.toContainText('有新回复');
  await expect(page.locator('#reading-status')).toContainText('已定位历史聚合记录');
  await expect(page.locator('#reading-status')).not.toContainText('聚合草稿');
});
