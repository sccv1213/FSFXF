# 分身斧修复工具

修复直播录播里"拼屏布局"的画面遮挡工具。V2 完全重写(架构重构,功能 1:1 保留)。

> 详细使用说明见 **《使用说明书.md》**。

## 解决的问题

主播把同一路竖屏视频左中右各放一份拼成 16:9(三格),或两路 8:9 拼成 16:9(两格)。
直播时画面四角/中间常有文字、礼物信息遮挡。

**修复原理**:所有格子内容逐帧相同 → 从干净格子"同一相对位置"裁一块像素,盖到被遮挡的位置,
覆盖上去的内容与真实内容逐像素一致,整段视频都成立、肉眼无损。

## 安装与运行

需要:Python 3.13+、ffmpeg/ffprobe(需安装并加入 PATH)

```bash
pip install av==18.0.0 PySide6==6.11.1 numpy==2.5.2     # av:视频解码/编码;PySide6:界面框架;numpy:帧转换
```

ffmpeg/ffprobe 为外部程序,需安装并加入 PATH(从 ffmpeg 官网下载,解压后把 `bin` 目录加入系统 PATH)。

- **日常使用**:双击 `launcher.pyw`(无控制台黑窗)
- **调试/看报错**:`python launcher.pyw`（控制台版，错误同时输出到终端并弹窗显示详情）
- **双击无反应**:启动时会自动检查 av/PySide6,缺失或损坏都会弹窗显示具体错误和安装命令
  ——先执行上面的 `pip install av==18.0.0 PySide6==6.11.1 numpy==2.5.2`;若弹窗和终端都没有输出,
  查看软件目录下的 `启动失败.txt`(仅弹窗不可用的极端情况才会写入)

## 使用流程

1. **打开视频**(Ctrl+O)→ 自动显示编码信息,默认套用"三格 9:16"布局
2. **选布局**:左栏"布局"下拉选 三格 9:16 / 两格 8:9 / **四格** / 无网格;**布局按片段生效**——
   播放头在哪个片段,切换布局就作用于哪个片段(同一视频可前段三格、后段两格共存,
   各段分界线独立设置);格子分界线不平整时,在**选择模式**下鼠标指向分界线直接拖动
   微调(可处理黑边/间隙/不等宽);左右边缘线 = 最左/最右格子边框,拖离原位即裁剪
3. **画补丁**(两种方式,任选其一):
   - 按 **目标模式**,在脏区域拖一个红色矩形 → 自动在干净格子生成绿色源矩形
   - 按 **源模式**,先框选干净区域(绿色)→ 自动映射到相邻格;右栏可随时改
     "来源格"、点"目标格…"按钮**多选覆盖的格子**(可同时盖多个格,不能选来源格),
     锁定对齐时矩形自动按"同一相对位置"重算(数学级对齐)
   - 四角/中间有多个遮挡就画多个补丁;解除"锁定对齐"可手动微调
4. **微调**:滚轮缩放画面;选择模式下放大后左键拖空白处平移,鼠标指向补丁框 → 方向键 ±1 像素微调、
   指向分界线 → 方向键移线;框选被限制在当前格/画面内,不会越界
5. **预览**:"预览当前帧"渲染当前帧,**左原画右修复并排对比**;跳到目标区域
   **未被遮挡**的时刻目视确认无差异(源与目标内容一致)即可
6. **跳转**:传输栏 4 个输入格(时/分/秒/帧)→ 点"跳转"定位;帧从 1 计数
7. **分段**(布局中途变化时):把播放头拖到变化点 → 点工具栏"分割"按钮
   (在播放头位置精确到帧添加分段线,自动跳片段页)→ 逐段勾选补丁。
   **不分段时补丁/复制规则默认应用于整个视频**;删除所有片段后自动回到"全视频"模式
8. **复制格子模式**:不需要画补丁框——右侧"复制"页添加规则("复制整格"按钮),
   选"来源格 X → 目标格 Y(可多选)",把 X 格整格内容复制覆盖到目标格;
   每条规则可勾选"左右翻转",目标格画面将水平镜像;规则锚定创建时的网格;
   勾选 = 在当前时间所在片段生效;画面上来源格绿框、目标格红框高亮(常显)
9. **渲染**:处理范围(时:分:秒:帧 格式)用"当前/开头/结尾"按钮快速设定;
    "添加当前到队列" → "开始处理"。多个视频可逐个加入队列批量处理;
    取消的任务保留在队列,可再次"开始处理"重跑

**预览**:空格播放带声音(传输栏音量条可调);←/→ 跳 1 秒,Ctrl+←/→ 跳 15 秒,
Shift+←/→ 跳 30 秒(悬停音量条时左右键调音量、悬停画面且选中补丁时微调矩形);
上一帧/下一帧按钮逐帧步进(点击自动暂停);跳转输入格无上下箭头、键盘直接输入。
**播放倍速**:传输栏 0.5倍/1倍/2倍/3倍/5倍 按钮,音视频同步变速(音调随倍速
变化属预期),新打开视频默认 1 倍。

## 画质策略

| 项 | 默认 | 说明 |
|---|---|---|
| 编码器 | 硬件优先 NVENC | 50 系显卡同码率画质与 x264 medium 无肉眼差异,速度快 5-10 倍;失败时弹窗三选一(软件重试/跳过/取消),不自动切换 |
| 视频码率 | 匹配原码率 | 体积 ≈ 原视频;可换 CRF 恒定质量(画质优先)或自定义 |
| 音频 | 流复制(零损失) | 默认不重编码;**处理范围不是全片时**,音频会单独重编码一次以对齐视频边界(码率参照源音频) |
| 色彩 | 透传元数据 | 不偏色 |
| 核验 | 自动对比输出参数 | 分辨率/帧率/码率偏差超限会告警 |

输出:`原文件名_修复.mp4`(同目录或自定义目录),原文件不动。

分段渲染采用帧号边界 + `-frames:v` 精确帧数;全片范围分段时视频分段编码、整条原音轨流复制合入,范围裁剪时音频整段重编码一次,避免 60FPS 变 59.9X。

## 工程文件

工程可保存为 `.fsfx.json`(Ctrl+S),下次打开(Ctrl+Shift+O)恢复全部补丁/片段/网格;
视频文件移动后重开工程会提示重新定位。**V2.1 工程格式 version=3**(自动迁移旧 v2 工程;更早的 v1 旧档需重建)。

## 模板与批量处理

- **保存模板**(传输栏,倍速右边):把当前时间所在分段的补丁/复制/边缘线/分界线保存为模板文件,
  存软件目录 `templates/<名>.fstpl.json`;**应用模板**:把模板替换到当前时间
  所在分段(全局模式 = 应用到全局),替换该处现有内容
- **批量导入视频**(队列面板):当前工程**无分段**时,一次选择多个视频,
  按当前工程的补丁/复制/网格/设置逐个处理(有分段时入口会提示先删除分段);
  每个视频输出到各自目录的 `原名_修复.mp4`

## 快捷键

| 键 | 功能 |
|---|---|
| Ctrl+O / Ctrl+S / Ctrl+Shift+O | 打开视频 / 保存工程 / 打开工程 |
| 空格 | 播放/暂停(任意焦点位置全局生效,预览带声音) |
| ← / → | 后退 / 前进 1 秒(Ctrl = 15 秒,Shift = 30 秒)——**未选中补丁且未悬停分界线时** |
| ←/→/↑/↓ | **选择模式下**:指向补丁框微调 ±1 像素(Shift = ±10 像素);指向分界/边缘线移线 |
| Esc | 取消当前绘制 |
| 滚轮 | 缩放画面(放大后选择模式左键拖空白处平移) |

## 架构(V2)

```
├── launcher.pyw  启动器(注入项目根 + 缺依赖探测/弹窗兜底)
├── main.py       入口(excepthook 兜底)
├── core/         纯 Python,零 Qt:rect/grid/project/planning/validation/render_commands/ffprobe/deps(依赖声明单一来源)
├── services/     Qt 服务:decoder(时钟+步进三拆)/player_controller/audio_output/render_controller/batch_queue
├── state/        app_state:工作流状态(Stage/EditMode/scope 派生/QSettings)
└── ui/           main_window(瘦身组装)/frame_view(绘制/交互三拆)/patch_panel/check_combo/timeline/queue/key_bindings
```

设计要点(详细设计文档见文末):补丁按 `anchor_grid` 字符串归属解析网格(无对象身份比较/引用计数);
`Scope` 收敛"隐式全段"判断;RenderController 单一状态枚举 + 转移表;PlaybackClock 显式状态机。

## 测试

```bash
python -m unittest discover tests   # 278 项（从项目根目录运行）
```

覆盖:模型/坐标对齐、edges 不变式、ffmpeg 命令构建、状态机转移表穷举(FakeProcess)、
PyAV 精确 seek、端到端渲染(像素级验证补丁区域与干净格一致、拼接空洞回归、crop 尺寸回归)、
方向键悬停分发、CheckCombo 交互、主窗口工作流。

CI:`.github/workflows/ci.yml` 在 Windows runner 上安装 Python 依赖与 ffmpeg/ffprobe 后
运行全量测试。

---

## 详细设计文档(原 CLAUDE.md)

# 分身斧修复工具 V2 — 项目文档

修复直播平台录播中"拼屏布局"（同一路 9:16 竖屏左中右各放一份 / 两路 8:9 / 四格）画面遮挡的工具。原理：格子内容逐帧相同 → 从干净格"同一相对位置"裁像素盖到脏位置，逐帧像素级一致。

**V2 = 完全重写**（旧版 V1 已删除，冻结代码可从 git 历史查阅）。功能与交互 1:1 保留，架构重构：旧版 21 条"靠回归测试钉住的教训"已全部做成结构性设计（见下文"设计决策"）。

## 运行与测试

```bash
python launcher.pyw    # 调试（控制台看报错）；日常双击同文件（无黑窗）
python -m unittest discover tests    # 278 项测试（从项目根目录运行）
```

环境：Python 3.14 + PySide6 6.11.1（已装）+ PyAV 18 + numpy 2.5.2 + 系统 ffmpeg（`D:\ffmpeg-master-latest-win64-gpl\bin`，PATH 中）。用户显卡 RTX 5060 Ti（NVENC）。音频：QAudioSink（QtMultimedia，用户实测有声）。

## 架构（项目根目录；2026-08-09 去掉 fenshenfu_v2 包层）

依赖方向 `ui → state → services → core`，禁止反向；`core` 零 Qt 依赖（全部单测不启动 QApplication）。

```
├── launcher.pyw          # 启动器:sys.path 注入项目根 + 缺依赖探测/三层弹窗兜底(不静默退出)
├── main.py               # 入口:excepthook 弹窗兜底 + 懒加载 MainWindow
├── core/                 # 纯 Python,零 Qt
│   ├── rect.py           # Rect + to_px(独立取偶:等宽⇒像素等宽,结构性保证)
│   ├── grid.py           # GridLayout:tiles 值语义 + move_edge 先算后写(唯一写入口)
│   ├── project.py        # Project/Segment(id!)/Patch(anchor_grid)/CopyRule/EncoderSettings
│   │                     #   + 全部模型变换(split_at/toggle/set_crop/realign)+ JSON v2
│   ├── planning.py       # Scope 第一类对象 + effective_scopes + anchor_for_scope
│   ├── validation.py     # validate(补丁N/复制规则N 序号报错,不用 uuid)
│   ├── render_commands.py# ffmpeg 命令纯函数(签名用 Scope)
│   ├── ffprobe.py        # 查找/MediaInfo/三级码率回退/色彩透传/nvenc_pix_fmt
│   └── deps.py           # 依赖声明单一来源:REQUIRED/PIP_INSTALL_HINT/check_deps
├── services/             # Qt 依赖服务
│   ├── decoder.py        # DecodeWorker(QThread)+ PlaybackClock + StepPlanner 三拆
│   ├── player_controller.py  # 播放桥接(原样保留)
│   ├── audio_output.py   # AudioOutput(QAudioSink:缓冲/flush 挂起/reset 重启)
│   ├── render_controller.py  # 单一状态枚举 + 转移表 + process_factory 注入
│   ├── batch_queue.py    # JobStatus/Job/JobQueue(纯数据容器)
├── state/app_state.py    # AppState:Stage/EditMode/current_scope 派生/QSettings/动作委托
└── ui/
    ├── main_window.py    # 仅 UI 组装 + 信号接线(~460 行,旧版 1269)
    ├── frame_view.py     # FrameView(画布)/FramePainter(纯绘制)/FrameInteractor(DragState 状态机)
    ├── check_combo.py    # CheckCombo(五条坑固化 docstring)
    ├── patch_panel.py    # 三页 + sync_patch_combos 局部刷新
    ├── timeline_widget.py / video_info_panel.py / queue_widget.py / widgets.py / style.py
    └── key_bindings.py   # KeyRouter:方向键悬停分发唯一实现
```

## 关键设计决策（V2 结构性设计，改动前必读）

1. **补丁归属 = anchor_grid 字符串**（替代旧版"对象引用即所有权 + 引用计数"）：`Patch.anchor_grid ∈ {"project"} ∪ {"segment:<seg_id>"}`，可序列化；解析唯一路径 `Project.resolve_grid(anchor_grid)`（段有网格用段网格、无网格继承全局、段已删除回退全局 + `stale_anchors()` 标记）；`realign_patches(grid_key)` 字符串比较派发（替代 `p.grid is grid` 身份过滤）。**补丁创建时 anchor = 当前 scope 的 grid_key**（`anchor_for_scope`）。
2. **Scope = 渲染/编辑唯一上下文**：`planning.effective_scopes(project)` 覆盖 process_range 无洞无重叠（段裁剪/重叠后段覆盖/间隙补空 scope）；UI/绘制/守卫/命中/校验只问 `AppState.current_scope()`——旧版散落 8 处的"无段=全部生效"判断收敛为单查询点；无段 → 单个 global scope（全补丁/全规则）；全局模式取消某项 → `materialize_global_segment`（隐式→显式唯一入口）。
3. **Segment.grid = None 显式继承**（值语义，无共享引用）：`Project.materialize_segment_grid(seg)` 拖内部分界线前物化克隆；`split_at` 两段各自 `grid.copy()`（新段必是新 id——anchor 引用安全）。**注意旧版"段网格被切布局替换后补丁冻结在旧网格"的语义已改变为"补丁跟随段新网格"**（差异记录见文末）。
4. **move_edge 先算后写**（GridLayout 唯一写入口）：新字段值全部先计算再一次性写回——旧版"先保存 nxt 旧右边界"的顺序 bug（相邻分界线随动）结构性不可能；三分支 clamp（边缘线 [0,0.5]/[0.5,1] + 最小格宽 2%）收敛一处；`check_invariants()` 性质测试（随机 1000 次 move 恒成立）。**曾试验 edges 列表表示，因格子间可带间隙（黑边布局）而放弃**（连续表示会把间隙当成格）。
5. **to_px 独立取偶**（等宽性结构性保证）：`w = round(nw*W) & ~1` 只取决于 nw——归一化等宽的两个矩形（补丁 src/dst）像素宽必等；渲染端 `min(sw,dw)` 保留为防御兜底。**曾试验"取偶联动"x2=round((nx+nw)*W)，w 随 nx 浮动破坏等宽性而放弃**。
6. **crop = project.grid 首/尾边派生**（不存独立字段）：`Project.set_crop(lo, hi)` 唯一裁剪写入口，clamp 全在 move_edge 一处，全网格同步（替代 apply_outer_edges 三处重复）；`_sync_crop_all_grids` 不再需要。
7. **RenderController 单一状态枚举**（IDLE/RUNNING/NVENC_PENDING）：`state`/`busy` 派生，无独立标志位；每个转移单一赋值——旧版"skip_job 漏复位 _running 队列永久卡死"结构性不可能；NVENC_PENDING 期间 `_advance()` 不推进（等用户三选一）；`process_factory` 注入假进程（转移表穷举测试，不依赖真实 ffmpeg）；`errorOccurred` 必须连（FailedToStart 不发 finished）；retry_software 用 `copy.copy(project)` + `replace(settings, sw)` 副本（绝不动活工程）；软件重试后 `_nvenc=False` 天然只问一次。
8. **PlaybackClock 显式状态机**（idle/running/paused）：`reset_to(pts)` 是唯一重建入口（open/seek/step 全走它）——旧版"重建时钟必须同时清 _clock_paused_at"的不变式结构性消失（暂停不可能残留到重建之后）。
9. **StepPlanner 步进意图序列化**：hist_pts/实测间隔/连点合并（pending）三路分支收敛为 `plan()`；后退历史不足走估算（不越界——回归：曾线程静默死亡）。
10. **decoder 精确 seek 流程**（原样保留）：`container.seek(pts, backward=True)` 落关键帧 + `demux(*[视频, 音频])` 重建（漏音频流丢声音）；`_emit_until_seek_pts` 解码路径回填 `_pts_hist`（VFR 也精确）；PyAV18 `to_ndarray()` 无 format 参数（fltp float32 planar 手动转交错 s16）；顶层 try/except → errorOccurred（线程异常绝不静默死）。
11. **渲染参数契约**（有 e2e 测试背书，原样保留）：`-ss` 在 `-i` 后（输出 seek）+ `-t`（本 ffmpeg 构建输入 seek + B 帧会多丢时长 → concat 拼接点空洞）；concat `-c copy`；crop 标签被补丁 crop 与 overlay 多消费者共享时 overlay main 必须显式 `scale` 固定尺寸（否则裁剪失效）；NVENC `p6/vbr/b:v/maxrate 1.5x/bufsize 2x/multipass qres/spatial_aq`，**码率目标模式绝无 -cq**（质量优先冲满 maxrate 码率虚高 47%）；色彩元数据透传；三级码率回退。
12. **CheckCombo 五条坑**（固化在 docstring）：①事件过滤器必须同时装 view 和 view.viewport() ②Python 引用必须保存防 GC ③press 阶段拦截手动 toggle ④单选逐项 setCheckState 不能 blockSignals（弹出不重绘）⑤150ms 防抖；**勾选/互斥只走 sync_patch_combos/sync_copy_combos 局部刷新——整表重建会销毁弹出中的下拉**。
13. **键路由三层**（KeyRouter + FrameView 焦点级 + QShortcut）：空格/秒跳 QShortcut(ApplicationShortcut)；方向键按悬停目标分发（AppState.hover_target：音量条 ±5 / 画面微调 / 放行秒跳与输入框光标，**FrameView enter/leave 事件上报 hover**——旧版 widgetAt 实时查）；微调 = 直接调 `FrameView.handle_key`（绝不用 sendEvent，防重入递归）；**FrameView.event() 保留焦点级 ShortcutOverride 拦截**（焦点在画面时的抢键语义，应用级过滤器覆盖不到）；主窗口 keyPressEvent 兜底秒跳。**第一轮优化：网格模式并入选择模式**——选择模式下悬停分界/边缘线 → 方向键移线/左键拖动，悬停补丁 → 方向键微调/左键拖动；F1-F6 快捷键取消。
14. **QSettings 隔离**：MainWindow 支持 settings_org/app 参数，测试用 `FenshenFuTest/test` 专用键；键仅 last_dir/volume/splitter_sizes（通用 get_setting/set_setting 存新键）。
15. **帧精确分段渲染**（2026-08-17 修复“分段后 60FPS→59.9X”）：模型保存 `process_range_frames` 与 `Segment.start_frame/end_frame`（0 基帧号），`split_at` 保留播放头实际 pts 不再用 `round(t*fps)/fps` 覆盖；命令层 `-ss/-t` 走 `fmt_ts()` 高精度（禁 `.3f`），CFR 追加 `-frames:v` 强制每段帧数，删除 `max(0.05, ...)`；`-frames:v` 与全量 e2e 验证输出总帧数/时长/fps 不变。
16. **音频与视频分离合入**（修复 concat 按“最晚结束流”对齐把时长撑长）：全片范围只分段编码视频，最后 `build_mux_original_audio_cmd` 把整条原音轨流复制合入（零损失）；范围裁剪时用 `build_audio_only_cmd` 把整个范围音频单独重编码一次（码率 = 源音频码率×1.1 余量）再 mux；不再逐段 `-c:a copy`。
17. **复制规则 anchor + 左右翻转**：`CopyRule.anchor_grid` 与补丁同模型（创建时锚定当前 scope 网格，UI/校验/渲染统一 `resolve_grid`）；`CopyRule.flip_horizontal` 新增，渲染 op 为 `OverlayOp`（NamedTuple 兼容旧 6 元组解包），翻转载剪后接 `hflip`；复制表增加“左右翻转”列。
18. **绘制契约**（用户明确要求保留）：复制高亮 = 来源/目标格**绿/红半透明填充 + 虚线框**（同格混色成黄是半透明叠加的必然，非 bug，不要"修"）；**每个 drawRect 前 setBrush(NoBrush)**（手柄 brush 残留曾致整格不透明黄填充）；零尺寸 dst = 补丁停用显式标记（绘制/命中/渲染跳过，validate 仍报"目标区域无效"与旧版一致）；框选起点 clamp 到 [0,1] 再查格；补丁/复制序号报错（不用 uuid）。
16. **失败处理原则（用户明确要求）**：不自动切换/不静默兜底——NVENC 失败弹窗三选一（软件重试/跳过/取消全部）；音频输出失败明示"预览将无声音"（状态栏橙色徽标）；旧工程文件(version<2 且无迁移路径)显式拒绝并弹"请重建"。
17. **UI 布局坑**：QHBoxLayout 行尾必须 addStretch(1)（剩余空间平均拉宽）；播放按钮纯文字不用 emoji（Windows 撑高按钮）；QColor 必须传对象（6.11 不接受 str）；QAudioSink 无 write()（sink.start() 返回 QIODevice）、reset() 置 Stopped 后必须立即 start() 重启（无声根因）、缓冲仅 0.25s → 待写缓冲 + 20ms flush（掏空后挂起定时器）。

## 与旧版的行为差异（V2 设计决定，勿"修复"）

1. **切布局后补丁跟随新网格**（旧：补丁冻结在旧网格对象）：补丁 anchor 是段/全局键，段网格被替换后解析到新网格——用户切布局后通常重建补丁，差异影响极小。
2. **段删除后补丁 anchor 悬空 → 回退全局网格 + stale 标记**（旧：网格对象冻结快照）：validate 不额外报错（避免阻断渲染），stale 仅影响 realign 派发。
3. **间隙内（播放头在段外）按 gap scope 显示/守卫**（旧：间隙按隐式全段显示全部补丁且禁止移边界）：现在间隙显示无补丁、允许移边界（更符合"间隙无补丁"语义）。
4. **新工程格式 version=3,兼容 v2 迁移**（用户确认 v1 旧档仍拒绝）：`migrate_project_dict` 迁移注册表,v2→v3 自动补新字段缺省值,v1 无路径弹窗拒绝。
5. **测试基建**：状态机用 FakeProcess 穷举转移表（毫秒级，不依赖真实 ffmpeg）；全量 278 项（迁移自旧版的语义 + 新增：edges 不变式/to_px 一致性/scope 解析/时钟状态机/键路由/CheckCombo 死区/补丁格内约束/帧边界与翻转）。

## 用户工作流（README 有完整版）

打开视频 → 选布局（按段生效）→ 目标模式画脏区域/源模式画干净区域（自动对齐）→ 预览当前帧（原画/修复并排对比）→ 双击时间轴或"分割"按钮分段（帧精确，自动跳片段页）→ 逐段勾选补丁 → 选择模式拖分界线/边缘线（边缘线移动=裁剪）→ 添加队列 → 开始处理。输出 `原名_修复.mp4`。

## 已知错误与修复记录（多轮排查结论，新会话必读）

1. **全取消目标格 → 点来源格 → 重勾选,补丁永久隐藏**(已修,回归测试
   `test_clear_targets_then_change_source_then_recheck`):全取消目标格会
   `p.dst` 清零;若随后切换来源格,`lock_align` 重算会用**零尺寸 dst** 调
   `align_rect` 把 `p.src` 也清零;重勾选目标格时 `align_from_src(零 src)`
   无法恢复 → 补丁永远不显示。**修复:零尺寸 dst 时跳过 lock 重算**
   (main_window._on_patch_source_changed)。排查教训:验证"恢复"路径必须
   包含完整用户操作链,不能只测直接调用。
2. **来源格下拉"递减切换(3→2→1)到第一项文字残留"(已修,根因=点击丢失)**:
   真实平台探测确认弹出层容器有 2px 边框/边距(offscreen 1px,DPI 缩放会
   放大),第一项上缘的点击落在容器上而非 viewport → `indexAt` 返回无效
   → 事件不拦截 → **点击整次丢失**,模型不变故文字停留在旧值。递减切换
   3→2→1 最后落在第一项时最明显(前两次点击都成功,最后一次被吞)。
   **修复(check_combo._ComboFilter)**:过滤器扩展到弹出层容器三层,坐标经
   `globalPosition()` 归一化后**夹进 viewport**(顶部=第一项、底部=末项),
   弹出层窗口内任何点击必命中某行;窗口外点击仍放行由 Qt 关闭弹出
   (Qt::Popup 语义)。回归测试 TestComboDeadZone 4 项(offscreen 可复现:
   死区取 viewport y=-1,offscreen 下 y=-2 已出窗)。文字刷新收敛为
   itemChanged→_on_item_changed 一处 + set_checked 显式刷新;保留
   hidePopup 刷新(防 editable combo 关闭弹出时 Qt currentText 写回
   lineEdit,2026-08-09 审查删除了 toggle_row/_fire 两处 no-op 防御)。
   诊断开关
   `FENSHENFU_DEBUG_COMBO=1`(控制台版输出 [combo] 日志)保留。
3. **CheckCombo 五条坑**(见决策 12)是历次回归的重灾区:列索引变更
   (加"复制"序号列后 sync_copy_combos 曾用错列导致互斥失效)、防抖
   时序、禁用项点击。修改表格列结构时必须同步检查所有 cellWidget 列引用。
4. **分界线移动后补丁框体越格(已修,回归测试 test_align_rect_clamps_overflow
   / test_draw_target_after_boundary_move)**:格子2|3 分界线左移后格子3
   变宽,目标模式画 dst 到格子3 最右边,dst 宽超过来源格(格子2 变窄)→
   align_rect 尺寸锁定把 src 按同尺寸映射,src 右边缘越过格子2|3 分界线,
   渲染时取到目标格像素(自我复制,修复错乱)。**修复(三层)**:
   ① align_rect/align_from_src 映射结果超出格子边界时**收窄贴格右**(相对
   位置不变);② 调用方(创建 _finish_rubber / frame_view._realign /
   project.realign_patches / main_window 两处下拉处理器)把另一矩形同步
   收窄保持 src/dst 同尺寸不变式;③ 目标模式自动来源格优先选**能容纳
   dst 宽度的最近格**(_pick_source_tile,装不下才退回最近格收窄)。
   渲染端 min(sw,dw) 仍为最终兜底。注意:src/dst 同像素尺寸是补丁原理
   不变式(逐像素复制),任何"缩放 src 适配格子"的修法都违背它。
5. **删除分段线三连合并多吞一段(已修,回归测试
   test_remove_segment_merge_user_scenario)**:4 段布局删线3 后删线1,
   剩余 3 段直接合并成 1 段全长。根因:remove_segment_merge 曾无条件
   吸收**左右两邻**(三连合并),而 UI"删除分段线"语义 = 删线右侧段并与
   线左侧段合并(segment_at 半开区间返回右侧段)——被删段左右都有邻居时
   多吞一段,跨度恰为处理范围时整段删光(无段全局模式=全长)。**修复**:
   remove_segment_merge 只合并**左邻**(线两侧两段合一);无左邻(首段,
   UI 不可达)向后合并右邻保持对称;唯一段直接删。合并覆盖整个处理范围
   → 无段全局模式的分支保留(两段删一段 = 用户主场景)。
6. **删除分段线后删除按钮残留在错位行(已修,回归测试
   test_segment_line_delete_button_no_residue)**:删除分段线后片段页
   行数收缩、行内容错位,原分段线行的删除按钮 cellWidget 残留在新行
   (分段/结尾行显示"删除"按钮,且闭包 t 仍指向已删除的线,点击会删错线)。
   根因:refresh_segments 非 line 行只 `setItem("—")` 不清 cellWidget。
   **修复:非 line 行先 `setCellWidget(row, 4, None)` 再 setItem**。
7. **launcher 缺依赖兜底 7 缺口(已修,/code-review 结论)**:①兜底写 启动失败.txt 无保护
   (只读目录 → PermissionError 穿透 → pythonw 静默退出)——修复为三层递进通知,每层各自
   try/except:tkinter 弹窗 → ctypes MessageBoxW(不写盘)→ 写文件(再失败也吞掉);
   ②弹窗只显示 e.name 丢弃 str(e)(DLL load failed 病因不可见)→ 逐行 包名+真实错误;
   ③缺 PyAV 走不到启动弹窗(decoder.py 守卫 import av)→ launcher 启动时 check_deps
   (core/deps.py,真实 import 探测,缺/损坏都拦);④只捕 ImportError(SyntaxError/QApplication
   构造失败静默)→ 全部 except Exception + 顶部先装 launcher excepthook;⑤README 调试指引
   与行为矛盾 → 弹窗含真实错误 + stderr 输出(print 加守卫:pythonw 下标准流可为 None,
   main.py:14 同类问题一并修);⑥启动失败.txt 残留不清理 → 启动检查通过即删除;
   ⑦版本固定串 5 处拷贝无同步 → core/deps.py 单一来源,README/使用说明书 版本串由
   tests/test_deps.py 钉死(改版本必须同步文档,否则测试红)。

## 备注

- `三拼工具.py` / `启动.bat` 是用户独立的批处理工具（与主工具无关，用户已关闭该话题，无需记录）
- 测试产物会生成在项目目录（test_修复.mp4 等），可清理；git 已托管（2026-08-09 删除 V1：app/、tests/、main.py、launcher.py；test.mp4 同批删除后按用户要求从 git 恢复）
- 2026-08-09 全项目审查清理（0809A1.0 之后）：align 零尺寸守卫 + align_rect_pair/align_from_src_pair 统一 7 处调用点、set_crop 段锚定补丁重算、check_combo 防御收敛为 itemChanged + hidePopup、render_controller 死派生 API / anchor_for_scope / from_px / last_frame / _fmt_time 删除、测试无效断言 4 处清理、make_media 收敛 tests/helpers.py、7 处 mkdtemp 加清理
- 2026-08-09 播放倍速（0.5/1/2/3/5x）：节流目标 wait_until(pts/rate) 统一缩放（视频/音频同钟，时钟三个重建点不动）+ 抽帧阈值按 rate 缩放 + 音频样本 _rate_samples 抽稀/重复（音调变化属预期）+ 打开视频重置 1x + 切倍率时时钟锚点按新倍率重设（否则降倍率要等"播放位置×倍率差"秒，画面卡住；回归测试 TestRateCommand 2 项）
- 2026-08-09 模板与批量处理：core/template.py（fenshenfu_template v2(兼容 v1 自动迁移)：grid+patches+copy_rules，anchor 归一化 project，存软件目录 templates/）；Project.apply_template(替换目标段/全局，克隆新 id + anchor=segment:id)、clone_for_video(批量：深拷贝/清段/范围全长)；批量入口需无分段工程（有段拒绝，不静默）；模板按钮在传输栏倍速右边，保存 = 当前时间所在分段的网格 + 该段启用补丁/复制（无段 = 全局）
- 2026-08-09 队列：queueFinished 统一完成弹窗（汇总成功 N/失败列表，不再逐任务弹窗）；已完成任务可移除（remove_queued→remove_job，QUEUED/OK 可移除，运行中拒绝）；批量探测黑窗修复（ffprobe 子进程加 CREATE_NO_WINDOW）+ 跳过原因输出队列日志（不静默）
- 2026-08-09 交互收敛（用户要求）：双击时间轴分割移除（分割只走工具栏"分割"按钮）；Delete 键删除补丁移除（补丁/复制/分段删除只走右栏删除按钮）；中键平移移除（平移只走选择模式放大后左键拖空白处）。新增《使用说明书.md》用户文档（README 有链接）
- 复制规则高亮 = 来源/目标格**绿/红半透明填充 + 虚线框**（用户明确要求保留填充；与补丁 src/dst 填充同格时混色成黄是半透明叠加的必然，非 bug；回归测试 `test_copy_highlight_has_fill` / `test_no_yellow_fill_with_selected_patch_and_copy`）

- 2026-08-17 分段帧数/时长漂移修复（用户报告 60FPS→59.9X）：见决策 15/16；回归测试 TestFrameAccurateCommands/test_frame_boundaries + test_main_window_smoke.test_copy_flip_checkbox_updates_rule
- 2026-08-17 代码审查修复轮：删除 Scope/Segment 半活 pts_ticks 字段;FrameIndex 去重 PTS/删除未用 for_cfr;Project.bump 移到 docstring 后并避免无效 bump;修复 VFR overlay filter label [s] 被 ffmpeg 当 stream specifier 的 bug(改 [vsel]);补 VFR 范围音频与 VFR overlay e2e;编码设置区/README/说明书注明"范围≠全片时音频重编码"。
- 2026-08-17 最终收尾轮：verify_output(expected_frame_count) 只告警不阻断;新增 e2e test_60fps_split_preserves_frame_count_and_fps 与 test_copy_flip_horizontal_pixel_match;工程 version=3(v2 自动迁移)/模板 version=2(v1 自动迁移),v1 工程无路径仍拒绝。
- 2026-08-17 P4 工程化轮：core/project.py/core/template.py 增加迁移注册表(migrate_project_dict/migrate_template_dict,当前版本直接通过,旧版无迁移路径仍显式拒绝);新增 .github/workflows/ci.yml(Windows runner + Python3.13 + PySide6/PyAV/numpy + choco ffmpeg + offscreen 全量测试)。
- 2026-08-17 P2 可维护性轮：ui/main_window_services.py（文件/工程/模板/预览/批量）、ui/edit_controller.py（补丁/复制命令收敛）、ui/hover_tracker.py（事件过滤器替代 enterEvent lambda）；MainWindow 1269 → 696 行，旧方法保留为薄包装兼容测试/调用。
- 2026-08-17 P3 性能轮：Project.revision + AppState.effective_scopes() 缓存（结构变化/fps 变化才重建）；FrameView.derived_patch_targets() 派生矩形缓存（scope/refresh 失效）；RenderController._merge_render_scopes() 合并时间连续且 overlay ops 完全相同的相邻 scope（gap/无工作段合并），测试 TestScopeMerge/TestDerivedPatchTargetsCache。
- 2026-08-17 VFR 精确帧切割：core/frame_index.py + services/frame_index.py（后台 demux 建真实 PTS 索引）；Project/Segment/Scope 增加 pts_ticks 边界；VFR 分段用 select(between(n,...),setpts=PTS-STARTPTS) 按帧号取帧 + -video_track_timescale + setts 修正末帧时长；范围裁剪音频用 atrim start_sample/end_sample；放弃单进程 filter graph 方案 C。回归测试 test_frame_index/test_render_commands.TestFrameAccurateCommands
