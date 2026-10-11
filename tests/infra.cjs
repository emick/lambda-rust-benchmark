const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const variants=['python','python-snapstart','rust'];
const templates = Object.fromEntries(variants.map(v=>[v,JSON.parse(fs.readFileSync(`cdk.out/Benchmark-${v}.template.json`))]));
const resources=(t,type)=>Object.values(t.Resources).filter(r=>r.Type===type);
test('three isolated and equivalent deployments',()=>{
 const hashes=[];
 for(const v of variants){
  const t=templates[v];
  const f=resources(t,'AWS::Lambda::Function');assert.equal(f.length,1);
  const p=f[0].Properties;assert.deepEqual(p.Architectures,['arm64']);assert.equal(p.Timeout,15);assert.equal(p.MemorySize,512);
  assert.equal(p.ReservedConcurrentExecutions,100);assert.equal(p.Layers,undefined);assert.equal(p.VpcConfig,undefined);
  assert.equal(p.Runtime,v==='rust'?'provided.al2023':'python3.13');
  assert.equal(p.SnapStart?.ApplyOn,v==='python-snapstart'?'PublishedVersions':undefined);
  if(v.startsWith('python'))hashes.push(p.Code.S3Key);
  assert.equal(resources(t,'AWS::DynamoDB::Table').length,1);
  assert.equal(resources(t,'AWS::DynamoDB::Table')[0].Properties.BillingMode,'PAY_PER_REQUEST');
  assert.equal(resources(t,'AWS::S3::Bucket').length,1);
  assert.equal(resources(t,'AWS::S3::Bucket')[0].Properties.VersioningConfiguration,undefined);
  assert.equal(resources(t,'AWS::ApiGatewayV2::Api')[0].Properties.ProtocolType,'HTTP');
  assert.equal(resources(t,'AWS::ApiGatewayV2::Integration')[0].Properties.PayloadFormatVersion,'2.0');
  assert.equal(resources(t,'AWS::ApiGatewayV2::Route')[0].Properties.RouteKey,'$default');
  assert.equal(resources(t,'AWS::Lambda::Alias')[0].Properties.ProvisionedConcurrencyConfig,undefined);
  const policy=resources(t,'AWS::IAM::Policy')[0].Properties.PolicyDocument.Statement;
  const storage=policy.filter(s=>JSON.stringify(s.Action).includes('dynamodb:')||JSON.stringify(s.Action).includes('s3:'));
  assert.deepEqual(storage.map(s=>s.Action).sort(),['dynamodb:PutItem','s3:PutObject'].sort());
  for(const resource of Object.values(t.Resources))if(resource.Properties?.Tags){
   const tags=resource.Properties.Tags;
   assert.ok(Array.isArray(tags)?tags.some(tag=>tag.Key==='Project'&&tag.Value==='lambda-rust-benchmark'):tags.Project==='lambda-rust-benchmark');
  }
 }
 assert.equal(hashes[0],hashes[1]);
});
