## CapsWriter-Offline

基于 [CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)（一个 PC 端的语音输入、字幕转录工具）修改而来

对于常见音视频格式，可以通过右键一键视频转文字／语音转文字，生成 txt 文件，并将识别结果复制到剪贴板

![](http://cbu01.alicdn.com/i1/2793632751/O1CN01N1DF8a1WC0bYipUb6_!!2793632751-0-cbucrm.jpg)

## 特性

- 将电脑端语音识别操作极致简化，无需安装，解压到任意目录，双击 bat 文件即可添加右键菜单进行使用
- 完全离线、无限时长、低延迟、高准确率、中英混输、自动阿拉伯数字、自动调整中英间隔
- 热词功能：可以在 `hot-en.txt hot-zh.txt hot-rule.txt` 中添加三种热词，客户端动态载入

## Windows懒人包

1. 请确保电脑上安装了 [Microsoft Visual C++ Redistributable 运行库](https://learn.microsoft.com/zh-cn/cpp/windows/latest-supported-vc-redist)
2. 服务端载入模型所用的 onnxruntime 只能在 Windows 10 及以上版本的系统使用
3. 服务端载入模型需要系统内存 4G，只能在 64 位系统上使用

## 功能：热词

如果你有专用名词需要替换，可以加入热词文件。规则文件中以 `#` 开头的行以及空行会被忽略，可以用作注释。

- 中文热词请写到 `hot-zh.txt` 文件，每行一个，替换依据为拼音，实测每 1 万条热词约引入 3ms 延迟

- 英文热词请写到 `hot-en.txt` 文件，每行一个，替换依据为字母拼写

- 自定义规则热词请写到 `hot-rule.txt` 文件，每行一个，将搜索和替换词以等号隔开，如 `毫安时  =  mAh` 

你可以在 `core_client.py` 文件中配置是否匹配中文多音字，是否严格匹配拼音声调。

检测到修改后，客户端会动态载入热词，效果示例：

1. 例如 `hot-zh.txt` 有热词「我家鸽鸽」，则所有识别结果中的「我家哥哥」都会被替换成「我家鸽鸽」
2. 例如 `hot-en.txt` 有热词「ChatGPT」，则所有识别结果中的「chat gpt」都会被替换成「ChatGPT」
3. 例如 `hot-rule.txt` 有热词「毫安时 = mAh」，则所有识别结果中的「毫安时」都会被替换成「mAh」

![]([assets/image-20230531221314983.png](http://cbu01.alicdn.com/i2/2793632751/O1CN016VzaFI1WC0bfinDWJ_!!2793632751-2-cbucrm.png))

## 打赏

如果你愿意，可以以打赏的方式支持我一下：

![](http://cbu01.alicdn.com/i2/2793632751/O1CN01awypv51WC0beuto7L_!!2793632751-0-cbucrm.jpg)
