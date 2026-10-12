import original from '../../playwright.config.js';
export default {
  ...original,
  webServer:{...original.webServer,cwd:'/Users/nineofour/Agent-Alfred-v11'},
  testDir:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/baseline-tests',
  outputDir:'/Users/nineofour/Agent-Alfred-v11/output/playwright/results',
};
