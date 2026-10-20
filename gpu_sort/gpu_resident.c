/* 常驻 GPU 排序（数据驻留 + GPU 填充/紧凑）—— 2026-10-20 E3213
 * 目标（用户）：减少 CPU 压力；GPU 数据驻留；"存算一体"（数据不来回搬）。
 * 与 vulkan_radix_sort.c 的区别：
 *   - pad 哨兵填充、count 清零、最终紧凑都搬到 GPU（fill.comp / compact.comp）；
 *   - 数据 load 一次后常驻 device 缓冲，`--loops k` 连续排序 k 次不再重传；
 *   - CPU 只做 65536 前缀和（~0.1ms）与最终按需 dump 文件。
 * 用法（Termux 宿主）：
 *   gpu_resident [n] [--file in out] [--loops k]
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <vulkan/vulkan.h>
#define CK(x) do { VkResult _r=(x); if(_r){fprintf(stderr,"vk %d @%s\n",_r,#x);exit(1);} } while(0)
static VkDevice dev; static VkPhysicalDevice phys; static VkQueue queue;
static VkCommandPool pool; static VkCommandBuffer cmd; static VkFence fence;
static VkDescriptorSetLayout dsl; static VkDescriptorPool dpool; static VkDescriptorSet dset;
static VkPipelineLayout pl; static VkPipeline p[7]; static VkShaderModule sm[7]; static VkPipelineCache pcache;
typedef struct { VkBuffer b; VkDeviceMemory m; void* p; VkDeviceSize sz; } Buf;
static Buf buf[4]; /* 0=data 1=pad 2=count 3=wpos/offsets */
static VkDeviceSize data_sz, pad_sz, aux_sz;
static int g_sorted_idx=0;
static char* rd(const char* p, size_t* n){FILE*f=fopen(p,"rb");if(!f){fprintf(stderr,"缺 %s\n",p);exit(1);}fseek(f,0,SEEK_END);long s=ftell(f);fseek(f,0,SEEK_SET);char*b=malloc(s);fread(b,1,s,f);fclose(f);*n=s;return b;}
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static void mk(VkDeviceSize sz, Buf* o){
  o->sz=sz; VkBufferCreateInfo ci={VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,NULL,0,sz,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,VK_SHARING_MODE_EXCLUSIVE,0,NULL}; CK(vkCreateBuffer(dev,&ci,NULL,&o->b));
  VkMemoryRequirements mr; vkGetBufferMemoryRequirements(dev,o->b,&mr);
  VkPhysicalDeviceMemoryProperties mp; vkGetPhysicalDeviceMemoryProperties(phys,&mp);
  /* ★[E3224] UMA 优先：DEVICE_LOCAL|HOST_VISIBLE|HOST_COHERENT（手机统一内存）。
     旧实现只挑 HOST_VISIBLE ⟹ 可能落到 system memory，GPU 随机写/首单缺页更贵。 */
  uint32_t idx=~0u;
  for(int pass=0; pass<2 && idx==~0u; pass++){
    for(uint32_t i=0;i<mp.memoryTypeCount;i++){
      if(!(mr.memoryTypeBits&(1u<<i))) continue;
      VkMemoryPropertyFlags fl=mp.memoryTypes[i].propertyFlags;
      int ok = pass==0
        ? ((fl&(VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT|VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))==(VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT|VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))
        : ((fl&(VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))==(VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT));
      if(ok){idx=i;break;}
    }
  }
  VkMemoryAllocateInfo ai={VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,mr.size,idx}; CK(vkAllocateMemory(dev,&ai,NULL,&o->m)); CK(vkBindBufferMemory(dev,o->b,o->m,0));
  CK(vkMapMemory(dev,o->m,0,sz,0,&o->p));
}
static VkDeviceSize _szof(VkBuffer b){ for(int i=0;i<4;i++) if(b==buf[i].b) return buf[i].sz; return aux_sz; }
static void up(VkBuffer b0,VkBuffer b1,VkBuffer b2,VkBuffer b3){
  VkDescriptorBufferInfo bi[4]={{b0,0,_szof(b0)},{b1,0,_szof(b1)},{b2,0,_szof(b2)},{b3,0,_szof(b3)}};
  VkWriteDescriptorSet w[4]; for(int i=0;i<4;i++) w[i]=(VkWriteDescriptorSet){VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,NULL,dset,(uint32_t)i,0,1,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,NULL,&bi[i],NULL};
  vkUpdateDescriptorSets(dev,4,w,0,NULL);
}
static double g_stage_ms[8]={0}; static int g_cur_stage=-1; static int g_fallback=0; static uint32_t g_sub=0; static uint32_t g_bits=0; static const char* g_stage_name[8]={"fill_count","count16","fill_pad","scatter","bitonic","compact","wpos0","-"};
static void dispatch(int pi, uint32_t gx, uint32_t pc[4]){
  VkCommandBufferBeginInfo b={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,NULL,0,NULL};
  CK(vkResetCommandBuffer(cmd,0)); CK(vkBeginCommandBuffer(cmd,&b));
  vkCmdBindPipeline(cmd,VK_PIPELINE_BIND_POINT_COMPUTE,p[pi]);
  vkCmdBindDescriptorSets(cmd,VK_PIPELINE_BIND_POINT_COMPUTE,pl,0,1,&dset,0,NULL);
  vkCmdPushConstants(cmd,pl,VK_SHADER_STAGE_COMPUTE_BIT,0,16,pc);
  vkCmdPushConstants(cmd,pl,VK_SHADER_STAGE_COMPUTE_BIT,16,4,&g_sub);
  vkCmdDispatch(cmd,gx,1,1);
  CK(vkEndCommandBuffer(cmd));
  VkSubmitInfo s={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&cmd,0,NULL};
  double _t0=now(); CK(vkQueueSubmit(queue,1,&s,fence));
  { VkResult _w=vkWaitForFences(dev,1,&fence,VK_TRUE,5000000000ull); if(_w==VK_TIMEOUT){fprintf(stderr,"FENCE_TIMEOUT\n"); fflush(stderr); exit(3);} if(_w!=VK_SUCCESS) CK(_w); }
  CK(vkResetFences(dev,1,&fence)); if(g_cur_stage>=0) g_stage_ms[g_cur_stage]+=(now()-_t0)*1e3;
}
static uint32_t _bits_for(uint32_t n, uint32_t span_bits, uint32_t* shift){
  /* ★[E3221] 值域感知：先减最小值再取高位；容量条件(均值+6σ<450) 与值域位宽同时约束。
     窄值域(有序/锯齿/多重复)不再因"高位全 0"塌缩成单桶；不可行 ⟹ 返回 0（调用方回退 CPU）。 */
  uint32_t max_bits = span_bits < 16u ? span_bits : 16u;
  if(max_bits < 8u) return 0;
  for(uint32_t bits=8; bits<=max_bits; bits++){
    uint32_t buckets=1u<<bits; double avg=(double)n/buckets;
    if(avg + 6.0*sqrt(avg) < 450.0){ *shift = span_bits - bits; return bits; }
  }
  return 0;
}
static int _cmp_u32(const void* a,const void* b){ uint32_t x=*(const uint32_t*)a,y=*(const uint32_t*)b; return (x>y)-(x<y); }
static void sort_once(uint32_t n){
  g_fallback=0;
  uint32_t* _d0=(uint32_t*)buf[0].p;
  uint32_t _mn=_d0[0], _mx=_d0[0];
  for(uint32_t _i=1;_i<n;_i++){ uint32_t _v=_d0[_i]; if(_v<_mn)_mn=_v; if(_v>_mx)_mx=_v; }
  uint32_t _span=_mx-_mn+1, _sbits=1; while(_sbits<32u && ((uint64_t)1u<<_sbits)<(uint64_t)_span) _sbits++;  /* ceil(log2(span))，64位防溢出 */
  uint32_t shift=0;
  uint32_t bits=_bits_for(n,_sbits,&shift);
  if(getenv("RES_DEBUG")){ fprintf(stderr,"[dbg] sort n=%u min=%u max=%u span=%u sbits=%u bits=%u shift=%u\n",n,_mn,_mx,_span,_sbits,bits,shift); fflush(stderr); }
  g_sub=_mn; g_bits=bits;
  if(bits==0){ qsort(buf[0].p,n,4,_cmp_u32); g_fallback=1; g_sorted_idx=0; return; }
  uint32_t buckets=1u<<bits, mask=buckets-1;
  if(getenv("RES_DEBUG")){ fprintf(stderr,"[dbg] buckets=%u\n",buckets); fflush(stderr); }
  uint32_t pc0[4]={buckets,0u,0u,0u};
  g_cur_stage=0; up(buf[2].b,buf[2].b,buf[2].b,buf[2].b); dispatch(3, (buckets+255u)/256u, pc0);        /* fill count=0 */
  g_cur_stage=1; up(buf[0].b,buf[2].b,buf[3].b,buf[3].b); uint32_t pc[4]={shift,n,mask,512u}; dispatch(0,(n+255u)/256u,pc); /* count16（bits 自适应） */
  uint32_t* cnt=(uint32_t*)buf[2].p; uint32_t maxb=0; for(uint32_t i=0;i<buckets;i++) if(cnt[i]>maxb) maxb=cnt[i];
  if(getenv("RES_DEBUG")){ uint64_t sum=0; for(uint32_t i=0;i<65536;i++) sum+=cnt[i]; fprintf(stderr,"[dbg] maxb=%u sum=%llu n=%u data0=%u\n",maxb,(unsigned long long)sum,n,((uint32_t*)buf[0].p)[0]); }
  if(maxb>512){ /* ★[E3220] 官方分布 few_uniq/equal/hisp 等低位/同高位聚簇会单桶溢出：如实回退 CPU qsort，不静默假装 GPU 赢 */
    qsort(buf[0].p,n,4,_cmp_u32); g_fallback=1; g_sorted_idx=0; return;
  } 
  g_cur_stage=6; { uint32_t pz[4]={buckets,0u,0u,0u}; up(buf[3].b,buf[3].b,buf[3].b,buf[3].b); dispatch(3,(buckets+255u)/256u,pz); } /* wpos 清零 */
  { uint32_t pos=0; for(uint32_t bb=0;bb<buckets;bb++){ uint32_t c=cnt[bb]; cnt[bb]=pos; pos+=c; } cnt[buckets]=pos; } /* offsets[0..buckets] */
  g_cur_stage=3; up(buf[0].b,buf[1].b,buf[2].b,buf[3].b); uint32_t pcs[4]={shift,n,mask,0u}; dispatch(5,(n+255u)/256u,pcs); /* scatter 紧凑 */
  g_cur_stage=4; up(buf[1].b,buf[2].b,buf[2].b,buf[2].b); uint32_t pcb[4]={0u,0u,0u,0u}; dispatch(6,buckets,pcb); /* 桶内变长 bitonic */
  g_sorted_idx=1; g_cur_stage=-1;                          /* compact GPU */
}
int main(int argc,char**argv){
  uint32_t n=(argc>1 && argv[1][0]!='-')?(uint32_t)atol(argv[1]):1000000u;
  const char*in=NULL,*out=NULL; int loops=1;
  for(int i=1;i<argc;i++){ if(!strcmp(argv[i],"--file")&&i+2<argc){in=argv[i+1];out=argv[i+2];} if(!strcmp(argv[i],"--loops")&&i+1<argc) loops=atoi(argv[i+1]); }
  data_sz=(VkDeviceSize)n*4; pad_sz=(VkDeviceSize)65536*512*4; aux_sz=(VkDeviceSize)(65536+1)*4;
  VkApplicationInfo app={VK_STRUCTURE_TYPE_APPLICATION_INFO,NULL,"resident",1,"resident",1,VK_API_VERSION_1_1};
  VkInstanceCreateInfo ic={VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,NULL,0,&app,0,NULL,0,NULL}; VkInstance inst; CK(vkCreateInstance(&ic,NULL,&inst));
  uint32_t pn=1; CK(vkEnumeratePhysicalDevices(inst,&pn,&phys)); VkPhysicalDeviceProperties props; vkGetPhysicalDeviceProperties(phys,&props); printf("GPU: %s\n",props.deviceName);
  uint32_t qn=0; vkGetPhysicalDeviceQueueFamilyProperties(phys,&qn,NULL); VkQueueFamilyProperties* qp=malloc(qn*sizeof(*qp)); vkGetPhysicalDeviceQueueFamilyProperties(phys,&qn,qp); int qf=-1; for(uint32_t i=0;i<qn;i++) if(qp[i].queueFlags&VK_QUEUE_COMPUTE_BIT){qf=i;break;}
  float pr=1; VkDeviceQueueCreateInfo dq={VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,NULL,0,(uint32_t)qf,1,&pr}; VkDeviceCreateInfo dc={VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,NULL,0,1,&dq,0,NULL,0,NULL,NULL}; CK(vkCreateDevice(phys,&dc,NULL,&dev)); vkGetDeviceQueue(dev,qf,0,&queue);
  VkCommandPoolCreateInfo pc={VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,NULL,0,(uint32_t)qf}; CK(vkCreateCommandPool(dev,&pc,NULL,&pool));
  VkCommandBufferAllocateInfo ca={VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,NULL,pool,VK_COMMAND_BUFFER_LEVEL_PRIMARY,1}; CK(vkAllocateCommandBuffers(dev,&ca,&cmd));
  VkFenceCreateInfo fc={VK_STRUCTURE_TYPE_FENCE_CREATE_INFO,NULL,0}; CK(vkCreateFence(dev,&fc,NULL,&fence));
  VkDescriptorSetLayoutBinding bnd[4]={{0,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1,VK_SHADER_STAGE_COMPUTE_BIT,NULL},{1,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1,VK_SHADER_STAGE_COMPUTE_BIT,NULL},{2,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1,VK_SHADER_STAGE_COMPUTE_BIT,NULL},{3,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1,VK_SHADER_STAGE_COMPUTE_BIT,NULL}};
  VkDescriptorSetLayoutCreateInfo dl={VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,NULL,0,4,bnd}; CK(vkCreateDescriptorSetLayout(dev,&dl,NULL,&dsl));
  VkDescriptorPoolSize ps={VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,4}; VkDescriptorPoolCreateInfo dpc={VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,NULL,0,1,1,&ps}; CK(vkCreateDescriptorPool(dev,&dpc,NULL,&dpool));
  VkDescriptorSetAllocateInfo da={VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,NULL,dpool,1,&dsl}; CK(vkAllocateDescriptorSets(dev,&da,&dset));
  VkPushConstantRange pcr={VK_SHADER_STAGE_COMPUTE_BIT,0,20}; VkPipelineLayoutCreateInfo plc={VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,NULL,0,1,&dsl,1,&pcr}; CK(vkCreatePipelineLayout(dev,&plc,NULL,&pl));
  { /* ★[E3215] 持久化 PipelineCache：首跑编译结果落盘，下次直接加载（砍冷启动） */
    FILE* pf=fopen("pipeline_cache.bin","rb"); void* pdata=NULL; size_t psz=0;
    if(pf){ fseek(pf,0,SEEK_END); psz=(size_t)ftell(pf); fseek(pf,0,SEEK_SET); pdata=malloc(psz); if(fread(pdata,1,psz,pf)!=psz) psz=0; fclose(pf); }
    VkPipelineCacheCreateInfo pcci={VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO,NULL,0,psz,pdata}; CK(vkCreatePipelineCache(dev,&pcci,NULL,&pcache)); free(pdata);
  }
  const char* spv[7]={"count16.spv","scatter16.spv","bitonic.spv","fill.spv","compact.spv","scatter_compact.spv","bitonic_var.spv"};
  for(int i=0;i<7;i++){ size_t s; char* c=rd(spv[i],&s); VkShaderModuleCreateInfo si={VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,NULL,0,s,(uint32_t*)c}; CK(vkCreateShaderModule(dev,&si,NULL,&sm[i])); free(c); VkComputePipelineCreateInfo cp={VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,NULL,0,{VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,NULL,0,VK_SHADER_STAGE_COMPUTE_BIT,sm[i],"main",NULL},pl}; CK(vkCreateComputePipelines(dev,pcache,1,&cp,NULL,&p[i])); }
  { size_t csz=0; if(vkGetPipelineCacheData(dev,pcache,&csz,NULL)==VK_SUCCESS && csz){ void* cd=malloc(csz); if(vkGetPipelineCacheData(dev,pcache,&csz,cd)==VK_SUCCESS){ FILE* cf=fopen("pipeline_cache.bin","wb"); if(cf){fwrite(cd,1,csz,cf);fclose(cf);} } free(cd); } }
  mk(data_sz,&buf[0]); mk(pad_sz,&buf[1]); mk(aux_sz,&buf[2]); mk(aux_sz,&buf[3]);
  /* ── 常驻 daemon：命令文件轮询；数据驻留 GPU，CPU 只发小命令 ── */
  int _di=-1; for(int _i=1;_i<argc;_i++) if(!strcmp(argv[_i],"--daemon")) { _di=_i; break; }
  if(_di>0){
    const char* cmdf=(_di+1<argc)?argv[_di+1]:"/sdcard/Download/Operit/jacobian/gpu_cmd.txt";
    const char* resf=(_di+2<argc)?argv[_di+2]:"/sdcard/Download/Operit/jacobian/gpu_res.txt";
    uint32_t cur_n=0; double last_ms=0;
    if(getenv("RES_DEBUG")){ fprintf(stderr,"[dbg] daemon init done\n"); fflush(stderr); }
    /* ★[E3216] 预热：首次 GPU dispatch 有驱动 JIT/时钟拉起 ~200ms；放 startup 隐藏，不污染第一单。
       ★必须给 1024 个**分散值**，否则未初始化=全 0 ⟹ 单桶溢出。 */
    if(getenv("RES_DEBUG")){ fprintf(stderr,"[dbg] warmup begin\n"); fflush(stderr); }
  /* ★[E3224/T-342] 大缓冲池预触碰：把 pad(128MB) 全量页错误摊到启动（CPU memset ~几十 ms），
     否则第一次大 n scatter 随机写未触碰页 ⟹ 首单 8M 266ms/16M ~1s 尖峰。 */
  memset(buf[1].p,0xFF,(size_t)pad_sz);
  memset(buf[2].p,0x00,(size_t)aux_sz);
  memset(buf[3].p,0x00,(size_t)aux_sz);
    { uint32_t _wn=(uint32_t)(buf[0].sz/4); if(_wn>10000000u) _wn=10000000u; uint32_t* _d=(uint32_t*)buf[0].p;
      for(uint32_t _i=0;_i<_wn;_i++) _d[_i]=_i*2654435761u; sort_once(_wn); for(int _i=0;_i<8;_i++) g_stage_ms[_i]=0; }
    if(getenv("RES_DEBUG")){ fprintf(stderr,"[dbg] warmup done\n"); fflush(stderr); }
    for(;;){
      FILE* cf=fopen(cmdf,"rb"); if(!cf){ struct timespec ts={0,5000000}; nanosleep(&ts,NULL); continue; }
      char line[1024]={0}; size_t r=fread(line,1,sizeof(line)-1,cf); fclose(cf); remove(cmdf);
      if(!r) { struct timespec ts={0,5000000}; nanosleep(&ts,NULL); continue; }
      char cmd[64]={0}, arg1[768]={0}; unsigned long a=0,b=0;
      if(sscanf(line,"%63s %767s %lu %lu",cmd,arg1,&a,&b)<1) continue;
      char out[1024]={0};
      if(!strcmp(cmd,"LOAD")){
        cur_n=(uint32_t)a; VkDeviceSize need=(VkDeviceSize)cur_n*4;
        if(need>buf[0].sz){ snprintf(out,sizeof out,"ERR maxn=%llu",(unsigned long long)(buf[0].sz/4)); }
        else {
          FILE* f=fopen(arg1,"rb"); if(!f) snprintf(out,sizeof out,"ERR open %s",arg1);
          else { g_sorted_idx=0; uint32_t chunk=(uint32_t)(b?b:4194304u); size_t done=0; char* dst=(char*)buf[0].p;
            while(done<need){ size_t want=need-done; if(want>chunk) want=chunk; size_t got=fread(dst+done,1,want,f); if(!got) break; done+=got; }
            fclose(f); snprintf(out,sizeof out,"OK loaded %zu/%llu",done,(unsigned long long)need); }
        }
      } else if(!strcmp(cmd,"SORT")){
        if(!cur_n) snprintf(out,sizeof out,"ERR no data");
        else { double t0=now(); for(int _i=0;_i<8;_i++) g_stage_ms[_i]=0; sort_once(cur_n); last_ms=(now()-t0)*1e3; snprintf(out,sizeof out,"OK sorted n=%u bits=%u fallback=%d %.1fms stages fillcount=%.1f count=%.1f fillpad=%.1f scatter=%.1f bitonic=%.1f compact=%.1f",cur_n,g_bits,g_fallback,last_ms,g_stage_ms[0],g_stage_ms[1],g_stage_ms[2],g_stage_ms[3],g_stage_ms[4],g_stage_ms[5]); }
      } else if(!strcmp(cmd,"DUMP")){
        FILE* f=fopen(arg1,"wb"); if(!f) snprintf(out,sizeof out,"ERR open %s",arg1);
        else { fwrite(buf[g_sorted_idx].p,4,cur_n,f); fclose(f); snprintf(out,sizeof out,"OK dumped %u",cur_n); }
      } else if(!strcmp(cmd,"VERIFY")){
        /* 校验：单调性 + 和/xor 指纹（不 dump 全量；CPU 单遍 ~几十 ms，仅按需调用） */
        uint32_t* d=(uint32_t*)buf[g_sorted_idx].p; int ok=1; uint64_t sum=0, xr=0;
        for(uint32_t i=0;i<cur_n;i++){ sum+=d[i]; xr^=d[i]; if(i && d[i]<d[i-1]) ok=0; }
        snprintf(out,sizeof out,"OK n=%u sorted=%d sum=%llu xor=%llu",cur_n,ok,(unsigned long long)sum,(unsigned long long)xr);
      } else if(!strcmp(cmd,"FP")){
        uint32_t* d=(uint32_t*)buf[g_sorted_idx].p; uint64_t sum=0, xr=0;
        for(uint32_t i=0;i<cur_n;i++){ sum+=d[i]; xr^=d[i]; }
        snprintf(out,sizeof out,"OK n=%u sum=%llu xor=%llu",cur_n,(unsigned long long)sum,(unsigned long long)xr);
      } else if(!strcmp(cmd,"STATS")){
        snprintf(out,sizeof out,"OK n=%u last_sort_ms=%.1f",cur_n,last_ms);
      } else if(!strcmp(cmd,"EXIT")){
        snprintf(out,sizeof out,"BYE"); FILE* rf=fopen(resf,"wb"); if(rf){fprintf(rf,"%s\n",out);fclose(rf);} break;
      } else snprintf(out,sizeof out,"ERR cmd %s",cmd);
      { char _tmp[1200]; snprintf(_tmp,sizeof _tmp,"%s.tmp",resf); FILE* rf=fopen(_tmp,"wb"); if(rf){ fprintf(rf,"%s\n",out); fclose(rf); rename(_tmp,resf); } }
    }
    return 0;
  }
  if(in){ FILE* f=fopen(in,"rb"); if(!f){fprintf(stderr,"无法读 %s\n",in);return 1;} fread(buf[0].p,4,n,f); fclose(f); }
  else { srand(7); uint32_t* d=(uint32_t*)buf[0].p; for(uint32_t i=0;i<n;i++) d[i]=(uint32_t)rand(); }
  double t0=now(), tfirst=0;
  for(int r=0;r<loops;r++){ double a=now(); sort_once(n); double b=now(); if(r==0) tfirst=b-a; }
  double dt=(now()-t0)/loops;
  int ok=1; uint32_t* d=(uint32_t*)buf[g_sorted_idx].p; for(uint32_t i=1;i<n;i++) if(d[i]<d[i-1]){ok=0;break;}
  printf("n=%u loops=%d 平均 %.1f ms · 首次 %.1f ms · 正确=%s\n",n,loops,dt*1e3,tfirst*1e3,ok?"YES":"NO");
  if(out){ FILE* f=fopen(out,"wb"); fwrite(buf[g_sorted_idx].p,4,n,f); fclose(f); printf("写出 %s\n",out); }
  return 0;
}
