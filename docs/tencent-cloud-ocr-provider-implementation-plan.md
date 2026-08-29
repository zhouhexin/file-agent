# 腾讯云 OCR Provider 替换实施方案

> 状态：基础通用文字识别已实施，腾讯云表格识别待实施  
> 更新日期：2026-08-29  
> 适用范围：File Agent 图片、扫描 PDF 的基础 OCR，以及后续可选的表格 OCR

## 1. 结论

本项目可以把基础 OCR 从本地 PaddleOCR 切换为腾讯云 OCR，并且不需要重构 LangGraph、
`extract-document-text` Tool、`document_pages`、分类、摘要或检索链路。现有代码已经通过
`OcrProviderProtocol` 和 `OcrService` 隔离了 OCR Provider，正确做法是在该边界内新增
`TencentCloudOcrProvider`，继续向下游返回当前统一 OCR 结果结构。

推荐第一阶段使用腾讯云 `GeneralAccurateOCR`（通用文字识别高精度版）作为基础 OCR Provider：

- 图片文件：读取经过 MIME 校验的图片，按 Base64 调用腾讯云。
- 扫描 PDF：继续由现有 PyMuPDF 按页渲染，每页调用一次腾讯云，保持页码证据稳定。
- 原生 PDF、DOCX、XLSX、TXT 等已有正文的文件：仍使用确定性本地解析器，不调用 OCR。
- 分类、摘要、Chunk 和检索：继续消费 `document_pages.text_content`，无需知道 OCR Provider。

腾讯云基础 OCR 不等于现有 PP-StructureV3 表格结构恢复。要完全移除 Paddle 相关能力，还需要在
第二阶段为 `RecognizeTableAccurateOCR`（表格识别 V3）增加独立表格 Provider；在此之前不能把
通用 OCR 的纯文本结果冒充表格单元格结构。

## 2. 当前代码依据

当前基础 OCR 调用链为：

```text
extract-document-text
-> apps/api/app/modules/files/extractors.py
-> build_default_ocr_service()
-> OcrService
-> PaddleOcrProvider / LlmOcrProvider
-> 统一 OCR 结果
-> document_pages.text_content + metadata_json
```

相关实现：

- `apps/api/app/modules/ocr/service.py`
  - 已定义 `OcrProviderProtocol`。
  - `OcrService` 已负责 Provider 编排。
  - `build_default_ocr_service()` 当前固定把 `PaddleOcrProvider` 作为主 Provider。
- `apps/api/app/modules/files/extractors.py`
  - 图片直接调用 `OcrService.extract_image()`。
  - 扫描 PDF 先按页渲染，再调用相同 OCR 服务。
  - `_page_from_ocr_result()` 已把正文、来源、质量分、文字块和警告写入页面元数据。
- `apps/api/app/core/config.py`
  - 当前只有 `OCR_ENABLED`、Paddle 模型源和 LLM fallback 配置，没有基础 OCR Provider 选择项。
- `extraction_config_hash()`
  - 当前没有把基础 OCR Provider 身份纳入图片和扫描 PDF 的复用指纹。若不修正，切换腾讯云后仍可能复用旧 PaddleOCR 结果。

## 3. 腾讯云接口选择

### 3.1 第一阶段：基础 OCR

使用 `GeneralAccurateOCR`：

- 适合文字较多、小字、模糊字、倾斜文本等学校材料场景。
- 返回文字行、置信度和坐标，可映射到现有 `blocks`。
- 接口域名为 `ocr.tencentcloudapi.com`。
- 当前项目应发送 `ImageBase64`，不生成公网 URL，也不要求把文件额外上传到 COS。

腾讯云官方限制要求以接口文档为准。当前高精度接口要求图片编码后不超过 10 MB，支持 PNG、JPG、
JPEG、BMP。项目必须在发送前校验真实 MIME、编码后大小和页面尺寸，不能只相信扩展名。

### 3.2 第二阶段：表格 OCR

使用 `RecognizeTableAccurateOCR`：

- 返回表格、单元格文字和单元格位置。
- 可对接现有结构化抽取模块的表格结构，而不是只拼接文本。
- 官方默认频率限制为 2 次/秒，因此必须使用独立并发预算。
- 腾讯云返回的 Excel Base64 只能作为派生件候选，不能覆盖原文件。

第一阶段上线时可以继续保留 PP-StructureV3 处理用户明确要求的结构化表格抽取；如果要求所有 OCR
都不再使用 Paddle，则必须完成第二阶段后再关闭 PP-StructureV3 和 PaddleOCR-VL。

## 4. 统一名词与目标架构

本方案统一使用以下名词：

- **基础 OCR Provider**：负责图片和扫描 PDF 页面的文字行识别。
- **腾讯云 OCR Provider**：`TencentCloudOcrProvider`，基础 OCR Provider 的在线实现。
- **本地 OCR Provider**：现有 `PaddleOcrProvider`。
- **表格 OCR Provider**：负责恢复单元格结构，不与基础 OCR Provider 混用。
- **OCR 外发授权**：部署级明确配置，允许把页面图片发送到腾讯云 OCR。

目标调用链：

```text
图片 / 扫描 PDF 页面
-> 文件类型与大小校验
-> OCR 外发授权校验
-> TencentCloudOcrProvider
-> 腾讯云 GeneralAccurateOCR
-> 统一文字块与质量结果
-> document_pages + OCR 审计元数据
-> 摘要 / 分类 / Chunk / 检索 / 证据回答
```

LangGraph 和 LLM 不接触 SecretId、SecretKey、Base64、SDK Client 或原始腾讯响应。

## 5. 配置设计

在 `Settings`、`.env.example` 和 `docs/runbook.md` 中增加：

```dotenv
# 基础 OCR Provider：paddleocr_cpu / tencent_cloud。
OCR_PROVIDER=tencent_cloud

# 文件内容外发必须显式授权；false 时腾讯 Provider 必须关闭式拒绝调用。
OCR_EXTERNAL_CONTENT_AUTHORIZED=false

# 腾讯云密钥只填写在服务器真实 .env 或密钥管理系统中，不得提交到 Git。
TENCENT_CLOUD_OCR_SECRET_ID=
TENCENT_CLOUD_OCR_SECRET_KEY=
TENCENT_CLOUD_OCR_REGION=ap-guangzhou
TENCENT_CLOUD_OCR_ENDPOINT=ocr.tencentcloudapi.com

# 第一阶段固定使用高精度版；后端只接受白名单值。
TENCENT_CLOUD_OCR_ACTION=GeneralAccurateOCR
TENCENT_CLOUD_OCR_TIMEOUT_SECONDS=30
TENCENT_CLOUD_OCR_MAX_RETRIES=2
TENCENT_CLOUD_OCR_MAX_QPS=2

# 腾讯云失败时是否允许回退本地 PaddleOCR；默认关闭，避免部署方误以为结果都来自腾讯云。
OCR_LOCAL_FALLBACK_ENABLED=false
```

要让扫描 PDF 确实进入腾讯云基础 OCR，还应设置：

```dotenv
DOCLING_ENABLED=true
DOCLING_OCR_ENABLED=false
```

`DOCLING_OCR_ENABLED=true` 时，Docling 可能先在本地完成扫描 PDF OCR，腾讯云 Provider 不会被调用。
Docling 本身仍可保留，用于读取 PDF/DOCX 原生结构。

若第一阶段继续保留本地结构化表格能力：

```dotenv
PP_STRUCTURE_ENABLED=true
STRUCTURED_EXTRACTION_VISION_PROVIDER=paddleocr_vl
```

若要求所有图片能力都不再使用 Paddle，在腾讯云表格 Provider 完成前应关闭：

```dotenv
PP_STRUCTURE_ENABLED=false
STRUCTURED_EXTRACTION_VISION_PROVIDER=disabled
```

关闭后将暂时失去现有复杂表格结构恢复和局部 VLM 重识别能力，不能只通过 `OCR_PROVIDER` 弥补。

## 6. 代码改造

### 6.1 依赖

在 `apps/api/pyproject.toml` 增加腾讯云官方分产品 SDK，避免安装体积更大的全产品包：

```toml
"tencentcloud-sdk-python-common>=3.1,<4.0",
"tencentcloud-sdk-python-ocr>=3.1,<4.0",
```

两个包应使用兼容版本。测试不能调用真实腾讯云。

### 6.2 Provider 文件

新增：

```text
apps/api/app/modules/ocr/tencent_cloud_provider.py
```

公开类：

```python
class TencentCloudOcrProvider:
    name = "tencent_cloud_general_accurate"

    def extract_image(
        self,
        *,
        image_path: Path,
        page_number: int = 1,
    ) -> dict[str, Any]:
        ...
```

职责边界：

1. 校验 `OCR_EXTERNAL_CONTENT_AUTHORIZED=true`。
2. 使用现有真实 MIME 检测，不相信文件扩展名。
3. 对腾讯云不支持的 TIFF、WebP 等格式生成临时 PNG/JPEG 派生输入，不修改原件。
4. 控制编码后大小；超限时按比例缩放或压缩临时副本，仍超限则返回结构化失败。
5. 通过官方 SDK 调用 `GeneralAccurateOCR`。
6. 把 `TextDetections` 转成现有统一 `blocks`。
7. 把腾讯云百分制置信度转换为 0 到 1。
8. 返回腾讯云 `RequestId`，只用于审计和故障定位。
9. 不把 Base64、OCR 全文、密钥或原始 SDK 响应写入日志。

统一输出示例：

```json
{
  "ok": true,
  "text": "按阅读顺序拼接的页面正文",
  "source": "tencent_cloud_general_accurate",
  "provider_name": "tencent_cloud",
  "provider_version": "GeneralAccurateOCR@2018-11-19",
  "provider_request_id": "腾讯云 RequestId",
  "quality_score": 0.94,
  "confidence": 0.96,
  "blocks": [
    {
      "text": "一行文字",
      "order": 1,
      "polygon": [[0, 0], [100, 0], [100, 30], [0, 30]],
      "confidence": 0.98,
      "role": "text"
    }
  ],
  "warnings": []
}
```

### 6.3 Provider Factory

修改 `build_default_ocr_service()`：

```text
OCR_PROVIDER=paddleocr_cpu
-> PaddleOcrProvider

OCR_PROVIDER=tencent_cloud
+ OCR_EXTERNAL_CONTENT_AUTHORIZED=true
+ 密钥完整
-> TencentCloudOcrProvider

OCR_PROVIDER=tencent_cloud
+ 授权或密钥缺失
-> 返回 OCR_EXTERNAL_CONTENT_NOT_AUTHORIZED / OCR_PROVIDER_CONFIG_INVALID
-> 绝不自动外发，绝不默默切换 Provider
```

如 `OCR_LOCAL_FALLBACK_ENABLED=true`，腾讯云的可重试技术失败最终仍失败后才调用本地 Provider，并在
结果中写入 `is_fallback=true` 和 `fallback_from=tencent_cloud_general_accurate`。

### 6.4 页面元数据

扩展 `_page_from_ocr_result()`，持久化以下轻量审计字段：

```text
ocr_provider
ocr_provider_version
ocr_provider_request_id
ocr_quality_score
ocr_confidence
ocr_is_fallback
ocr_warnings
ocr_blocks
```

正文仍只进入 `document_pages.text_content`。普通日志和 AgentGraphState 不保存 OCR 正文、Base64 或坐标全集。

### 6.5 解析复用指纹

必须修改 `extraction_config_hash()`，将以下内容纳入图片和可能进入 OCR 的 PDF 指纹：

```text
OCR_PROVIDER
腾讯云 Action 与 API Version
图片预处理版本
DOCLING_OCR_ENABLED
本地 fallback 是否启用
```

示例身份：

```text
ocr-provider=tencent_cloud
action=GeneralAccurateOCR
api=2018-11-19
preprocess=tencent-ocr-image-v1
local-fallback=0
```

否则系统会把旧 PaddleOCR 页面判定为可复用结果，部署者会误以为腾讯云没有生效。

切换 Provider 后不删除历史 `document_extraction_runs`。新读取或管理员重处理生成新的运行；旧运行继续用于审计。

## 7. 错误、重试与限流

腾讯云错误分为三类：

### 7.1 可重试技术错误

- `InternalError`
- `ServiceUnavailable`
- `RequestLimitExceeded`
- `FailedOperation.EngineRecognizeTimeout`

最多重试 `TENCENT_CLOUD_OCR_MAX_RETRIES` 次，使用指数退避和随机抖动。每次重试仍受 Tool/异步任务预算约束。

### 7.2 不可重试输入错误

- 图片为空、模糊、无文字、解码失败。
- 文件或请求包超限。
- 参数错误。

转换为 `OCR_INPUT_INVALID`、`OCR_NO_TEXT`、`OCR_IMAGE_TOO_LARGE` 等内部稳定错误码，不盲目重试。

### 7.3 配置与账户错误

- 签名失败、密钥不存在、未授权。
- OCR 服务未开通。
- 欠费或资源包耗尽。

立即失败并给运维日志记录腾讯云错误码、RequestId 和建议动作；不得把 SecretId、SecretKey 或文件正文写入日志。

多 worker 部署时，仅有进程内限流不能保证账户总 QPS。第一阶段应把 OCR worker 并发限制为 1，并使用
`TENCENT_CLOUD_OCR_MAX_QPS` 做保守限流；后续多 worker 扩容时增加 Redis 或数据库共享限流器。

## 8. 外发授权与安全

腾讯云 OCR 会接收页面图片，其中可能包含学生个人信息，因此必须满足：

1. `OCR_EXTERNAL_CONTENT_AUTHORIZED` 默认 `false`。
2. 部署方明确设为 `true` 后，才允许后台 OCR 外发；这属于项目规则允许的“明确配置授权”。
3. 使用 CAM 子账号和最小权限，只允许所需 OCR Action；结合部署出口 IP 白名单。
4. SecretKey 只能放在真实 `.env`、容器 Secret 或服务器密钥管理系统，不能进入数据库普通设置、前端、日志和 Git。
5. 不使用主账号长期密钥。
6. 不关闭 TLS 证书验证。
7. 普通日志只记录 Provider、Action、RequestId、耗时、页码、状态和错误码。
8. 腾讯返回内容仍视为外部数据，不能作为系统指令。

若没有部署级外发授权，用户单次要求调用在线 OCR 时必须先生成 OperationPlan 并确认；后台导入任务不得自行弹出或绕过确认。

## 9. 表格与结构化抽取边界

基础 OCR 只保证文字行和位置，不保证：

- Excel 式行列结构。
- 合并单元格关系。
- 表格标题与表头层级。
- 公式、印章、图表和字段级业务结构。

第二阶段应新增 `TencentCloudTableOcrProvider`，把 `RecognizeTableAccurateOCR.TableDetections[].Cells`
映射到现有结构化抽取 schema。腾讯返回的表格坐标、行列范围和单元格文本属于确定性 Tool 输出；LLM
只能在这些已验证结果上做字段映射和总结，不能自行补造单元格。

## 10. 测试方案

所有自动测试使用 deterministic fake SDK Client，不调用真实腾讯云：

1. Provider 响应映射：文字、顺序、坐标、置信度和 RequestId。
2. 腾讯百分制置信度正确转换到 0 到 1。
3. MIME 伪装文件在外发前被拒绝。
4. 未授权、缺 SecretId 或缺 SecretKey 时关闭式失败。
5. Base64 超限时压缩临时副本；仍超限时返回稳定错误。
6. 可重试错误按上限退避；鉴权和输入错误不重试。
7. 日志不包含 Base64、密钥和 OCR 正文。
8. 图片 OCR 正确写入 `document_pages`。
9. 扫描 PDF 保持正确页码并逐页写入证据。
10. Provider 配置变化会改变 `parser_config_hash`，不会复用旧 PaddleOCR 结果。
11. 原生文本 PDF 不调用腾讯云。
12. 本地 fallback 默认关闭，显式开启后才允许执行。

上线前使用已脱敏且获得授权的代表性材料做人工烟测，至少覆盖：

- 清晰中文通知。
- 小字、多栏和倾斜扫描件。
- 多页扫描 PDF。
- 图片表格。
- 模糊图片和空白图片。
- 超大图片。
- 腾讯云限流、超时、欠费和密钥错误。

仓库提供不含真实个人信息的确定性扫描材料生成脚本：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/generate_tencent_ocr_test_document.py
```

脚本生成 `docs/test-data/tencent-cloud-ocr-test-page.png` 和不含文本层的
`docs/test-data/tencent-cloud-ocr-test-scanned.pdf`。识别结果可与
`docs/test-data/tencent-cloud-ocr-expected.txt` 对照；生成的 PNG/PDF 属于本地烟测产物，不要求提交到 Git。

## 11. 实施顺序

### 阶段一：基础 Provider

1. 增加配置和官方 SDK 依赖。
2. 实现 `TencentCloudOcrProvider` 与输入预处理。
3. 修改 Provider Factory 和页面审计元数据。
4. 修复 OCR 解析复用指纹。
5. 增加 deterministic fake 测试。
6. 更新 `.env.example`、README 和 runbook。

### 阶段二：测试环境验证

1. 在腾讯云开通 OCR。
2. 创建 CAM 子账号和最小权限密钥。
3. 测试环境设置 `OCR_PROVIDER=tencent_cloud` 和外发授权。
4. 设置 `DOCLING_OCR_ENABLED=false`，确认扫描 PDF 确实走腾讯云。
5. 对代表性文件比较正文完整度、页码、耗时和费用。

### 阶段三：生产切换

1. 先限制 OCR worker 并发为 1。
2. 只重处理一小批已授权文件，确认错误率和账户用量。
3. 再将生产环境 `OCR_PROVIDER` 切换为 `tencent_cloud`。
4. 历史文件按访问触发或后台低优先级批次重新 OCR，不在服务启动时同步重跑全部文件。

### 阶段四：腾讯云表格 Provider

1. 实现 `RecognizeTableAccurateOCR` 适配器。
2. 映射单元格、行列范围和表格位置。
3. 通过结构化抽取回归测试后，再评估关闭 PP-StructureV3 和 PaddleOCR-VL。

## 12. 验收标准

- 配置腾讯云 Provider 后，普通图片和扫描 PDF 页确实调用腾讯云而不是 PaddleOCR 或 Docling OCR。
- 未授权或密钥缺失时没有任何外发请求。
- OCR 正文、页码、坐标和 Provider 审计信息可以持久化并被分类、检索和证据回答复用。
- Provider 切换后不会错误复用旧 PaddleOCR 解析运行。
- 腾讯云失败不会让原件丢失或被修改。
- 日志不包含文件正文、Base64 和密钥。
- 所有测试使用 fake Client，测试环境不产生腾讯云费用。
- 表格结构能力未实现腾讯适配器前，不宣称腾讯通用 OCR 已替代 PP-StructureV3。

## 13. 官方参考

- [腾讯云通用文字识别（高精度版）](https://cloud.tencent.com/document/product/866/34937)
- [腾讯云通用印刷体识别](https://cloud.tencent.com/document/api/866/33526)
- [腾讯云表格识别（V3）](https://cloud.tencent.com/document/api/866/86721)
- [腾讯云 OCR API 概览](https://cloud.tencent.com/document/api/866/33515)
- [腾讯云 OCR 错误码](https://cloud.tencent.com/document/api/866/33528)
- [腾讯云 Python SDK](https://cloud.tencent.com/document/sdk/python)
- [腾讯云 OCR CAM 权限说明](https://cloud.tencent.com/document/product/598/107102)
- [腾讯云 Python OCR SDK 包](https://pypi.org/project/tencentcloud-sdk-python-ocr/)
