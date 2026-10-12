import original from '../../../playwright.config.js';
export default {...original,testDir:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/ci-repair/probe-tests',outputDir:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/ci-repair/measurements/'+process.env.V11_RECEIPT_LAYOUT,webServer:{...original.webServer,cwd:'/Users/nineofour/Agent-Alfred-v11'}};
