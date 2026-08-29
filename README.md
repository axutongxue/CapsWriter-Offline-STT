软件纯为自用项目，不保证后续维护与更新，问题反馈请到公众号留言

# 下载安装

下载软件压缩包，解压到一个固定文件夹，后续不要移动

右键下图的 bat 文件，选择管理员模式运行

![](https://wework.qpic.cn/wwpic3az/572492_upC-U0-GQyu3GHd_1782313193/0)

接着就会出现一个黑色弹窗，表示成功往右键菜单添加了软件功能

![](https://wework.qpic.cn/wwpic3az/506899_I6JjbF0CQ-q0cfj_1782313193/0)

至此软件安装就完成了，对，就这么简单![](https://wework.qpic.cn/wwpic3az/538446_VWyd-8ORQwiPAwv_1782313193/0)

# 使用方法

后续使用属于相当无脑，对着视频（或者音频）文件直接右键，选择 **CapsWriter 音视频转文字**

软件就会自动在后台启动并进行转录，同时现在「模型加载」和「转录过程」都加上了**浮窗提示**，更加直观![](https://wework.qpic.cn/wwpic3az/546045_WpKqVlYMR1Cs-6j_1782313193/0)

![](https://wework.qpic.cn/wwpic3az/184720_hZ1IahjzRg6vxWj_1782313518/0)

除了视频，**音频转文字**也是完全支持的。如果你下载过一些播客，这个软件也用得上

![img](https://wework.qpic.cn/wwpic3az/542654_1pzLTdD-SV-FqFR_1782313193/0)

**软件还支持同时选中多个文件，右键进行批量转录**，总之就是真 · 一键操作![](https://wework.qpic.cn/wwpic3az/600602_sO3ILP3VSeikQKU_1782313193/0)

默认的话会导出 SRT 字幕和纯文本，可以根据自身需要去系统托盘图标处设置输出格式

![](https://wework.qpic.cn/wwpic3az/573650_eIBowR5iR4CnFb1_1782313193/0)

同时软件内置 **Qwen3-ASR-1.7B、Fun-ASR-Nano** 两种语音识别模型

默认使用 Qwen3-ASR，支持多语种且中文方言支持更多，准确率会更高一点

![](https://wework.qpic.cn/wwpic3az/642608_InUEJGttTnyoBUA_1782313193/0)

但 Fun-ASR-Nano 也支持 7 大方言外加 26 地域口音，其实有些时候反而识别效果会更好

并且 **Fun-ASR-Nano 内存占用会更低一点，对低配机器更友好**，所以大家可以按自己实际情况来选择![](https://wework.qpic.cn/wwpic3az/558838_Wj1sd1vFR0yrhbR_1782313193/0)

# 关于GPU显卡加速

2.3版新增了GPU显卡加速控制功能

开启 ONNX 编码加速即可使用 DirectML 显卡加速

![](https://img30.360buyimg.com/imgzone/jfs/t20271003/505694/33/14704/49186/6a9274f3Fe3cab061/0936346196d32b6b.png)

但据上游反馈，有朋友实测他的 AMD 集显、AMD 独显在打开 DirectML 加速后，反而可能导致变慢，所以为了让这部份用户开箱可用，ONNX 编码加速默认是关的，需手动勾选打开

其次由于 DirectML 显卡加速是短突发负载（每段 30-60s 音频编码只花几十毫秒），任务管理器采样间隔 1 秒根本捕不到这种瞬时占用

所以如果你想知道GPU显卡加速是否生效，可以看耗时对比，20 分钟音频在 RTX 5060 Ti 上转码仅需2分钟不到，纯 CPU 不可能这么快，耗时起码多几倍

如果你想通过看 GPU 占用来判断，也不能简单的直接看任务管理器，需要手动切换到 Compute_0 或者 Compute_1 观察占用情况

![](https://img30.360buyimg.com/imgzone/jfs/t20271003/514620/28/3089/146080/6a927597F411f5d47/09366dc45a23c4e2.png)

另外**部分 Nvidia 独显用户**，可能由于未知的系统问题，导致无法使用 Vulkan 加载模型，此时请将 GGUF解码用显卡给取消勾选

而**部分集显用户**，可能会 Vulkan 载入模型失败，此时请将「集显补丁：禁用 COOPMAT」给勾选上，或者遇到「解码有误，强制熔断」的输出，此时请将「集显补丁: 禁用 F16」给勾选上，不确定就先勾一个试，无效再加另一个

# 补充说明

软件首次使用时可能会弹出防火墙弹窗，这个直接选允许访问即可

![](https://wework.qpic.cn/wwpic3az/628131_Rgyy1fISR3WDlR-_1782313193/0)

仅是软件会在本地跑一个后台服务器，不涉及任何网络传输行为，你把**「电脑断网」**软件一样可正常使用

如果你想问**在线视频**怎么转文字呢？

请看我之前发的这篇文章[《各平台官方免费视频转文字＋AI总结方法》](https://mp.weixin.qq.com/s/b2ku0b6VMbMX133GAvfOrg)

![](https://wework.qpic.cn/wwpic3az/694157_mSJic5FtT6yCURk_1782313193/0)

# 关于作者

![](https://wework.qpic.cn/wwpic3az/204530_pPKgyISBRuS8evJ_1782314026/0)
