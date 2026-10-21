# 累计表格单一工作副本发布方案

## 目标

共享累计表格只发布为一个普通工作副本。文件列表始终只显示一条，例如
`登记汇总.xlsx`；后续追加数据并导出时，为同一 `Document` 创建新的
`DocumentVersion`，更新同一 `WorkingCopy.current_version_id`，不再额外维护一份
`FILE_STORAGE_ROOT/derivatives/structured-datasets` 导出文件。

## 存储与身份

- 逻辑文件：一个 `Document`、一个当前 `FileObject`、一个 `ManagedFile`、一个 `WorkingCopy`。
- 可见根：`WORKING_COPY_STORAGE_ROOT/structured-datasets/`。
- 版本内容：
  `structured-datasets/{dataset_id}/.versions/{version_number}-{export_id}.xlsx`。
- 系统新建目标的当前可见文件：`structured-datasets/{dataset_id}/{用户可见文件名.xlsx}`。上传 Excel 注册为
  目标时复用其现有受控 WorkingCopy 路径，不能复制出第二个普通文件。两者的 basename 都必须与文件列表、
  下载名和后续按文件名引用完全一致。
- 当前下载：通过工作副本当前版本或当前 FileObject 读取；历史版本信息由
  `DocumentVersion` 保存，历史内容保持不可变。
- `StructuredDataset.working_copy_id` 固定数据集与唯一工作副本的关系。
- `StructuredDatasetExport.document_version_id` 固定数据集 revision 与文件版本的关系；
  `storage_path` 改为工作副本根内的受控相对路径。

`.versions` 中的文件是同一逻辑文件的内部版本，不作为独立工作副本显示。系统不再为
同一次导出同时保留 derivative 快照和 WorkingCopy 两个文件对象。

## 发布行为

1. `structured_dataset_export` 锁定数据集，并按当前 revision 幂等复用既有版本。
2. 服务端生成 XLSX 到新的不可变版本路径。
3. 首次导出创建系统生成根、`ManagedFile`、`Document`、`DocumentVersion`、
   `WorkingCopy` 和初始路径审计。
4. 后续导出只新增 `DocumentVersion`，更新原 `WorkingCopy` 的当前版本、大小、哈希和
   revision，并原子刷新同名当前可见文件；文件列表不会增加第二条记录。
5. 生成 `ChangeSet/ChangeItem`，回执返回 `working_copy_id`、`document_id`、
   `document_version_id` 和版本号。
6. MCP 可以继续下载当前导出作为宿主交付副本；该下载不构成第二个服务端业务文件。

发布协调器完成同名可见文件和版本核验后，必须在标记发布完成的同一数据库事务中，为当前
`WorkingCopy` 创建或更新 `document_search_profiles`。投影的 `document_version_id` 必须等于
`WorkingCopy.current_version_id`，否则精确文件名检索的当前版本校验会丢弃结果。用新目标替换
旧目标时，同一事务将旧工作副本的投影标记为 `INACTIVE`。已发布但缺少投影的历史目标，可用
`app.scripts.rebuild_dataset_search_profile` 按工作副本 ID 和冻结版本 ID 幂等补建；补建只改检索
派生数据，不创建 Excel 新版本。

## 失败与兼容

- 文件写入采用原子 XLSX writer；数据库事务失败时不把未关联文件暴露为工作副本。
- 旧 `StructuredDatasetExport` 没有 `document_version_id` 时，首次再次导出会把同一
  revision 发布为工作副本版本并更新旧导出记录。
- 普通文件删除、移动、独立重命名仍沿用 WorkingCopy 的 OperationPlan 规则；
  数据集当前可见文件的改名同步 `StructuredDataset.target_filename`，但不移动 `.versions` 历史内容；
  数据集自身生成新版本属于用户已要求的导出动作，不追加第二次确认。

## OCR 边界

`OCR_PROVIDER` 只控制 File Agent 服务器内部 OCR。WorkBuddy 外部 OCR Provider 由插件
Skill 选择；当前插件显式选择 `tencent-docs` 的 `ocr.extract`，两者互不替代。
