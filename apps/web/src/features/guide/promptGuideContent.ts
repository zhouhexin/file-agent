// 提示词指南只描述已经落地的用户能力；入口差异通过 channel 明确展示，避免混用附件和授权目录。

export type PromptGuideChannel = '网页聊天 / WorkBuddy' | 'WorkBuddy MCP' | 'WorkBuddy 附件套件';

export type PromptGuideItem = {
  id: string;
  title: string;
  prompt: string;
  channel: PromptGuideChannel;
  note?: string;
};

export type PromptGuideSection = {
  id: string;
  title: string;
  description: string;
  items: PromptGuideItem[];
};

export const PROMPT_GUIDE_SECTIONS: PromptGuideSection[] = [
  {
    id: 'search',
    title: '查找文件',
    description: '按主题、年份或完整文件名查找已经入库的文件。',
    items: [
      {
        id: 'search-topic',
        title: '按主题查找',
        prompt: '请用文件助手查找2025年人才引进相关文件，并说明每个文件的命中依据。',
        channel: '网页聊天 / WorkBuddy',
      },
      {
        id: 'search-exact-name',
        title: '按完整文件名查找',
        prompt: '请用文件助手精确查找“01引进人才工作合同-王磊磊.doc”。',
        channel: '网页聊天 / WorkBuddy',
        note: '建议保留扩展名；存在多个同名文件时，系统会要求选择。',
      },
      {
        id: 'search-title-signal',
        title: '按标题信号查找',
        prompt: '请用文件助手查找包含“教师职务聘期任务书”的文件，并说明命中依据。',
        channel: '网页聊天 / WorkBuddy',
      },
    ],
  },
  {
    id: 'read',
    title: '预览、下载与阅读',
    description: '先搜索确定文件，再预览、下载或读取正文。',
    items: [
      {
        id: 'preview-file',
        title: '预览文件',
        prompt: '请用文件助手预览“学校推荐意见.docx”。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'download-file',
        title: '获取下载入口',
        prompt: '请用文件助手给出“学校推荐意见.docx”的下载链接。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'read-search-result',
        title: '读取搜索结果',
        prompt: '请用文件助手读取刚才搜索结果中的第一个文件，并总结主要内容。',
        channel: '网页聊天 / WorkBuddy',
        note: '“第一个文件”必须引用同一轮搜索结果，不能脱离上下文使用。',
      },
    ],
  },
  {
    id: 'evidence',
    title: '证据问答与内容提取',
    description: '要求结论同时返回原文、页码、Sheet 或单元格位置。',
    items: [
      {
        id: 'evidence-requirements',
        title: '提取申报条件',
        prompt: '请用文件助手查看“人才项目申报书.pdf”，告诉我申报条件，并列出原文依据和页码。',
        channel: '网页聊天 / WorkBuddy',
      },
      {
        id: 'evidence-fields',
        title: '提取关键字段',
        prompt: '请用文件助手从刚才找到的通知中提取截止日期、联系人和联系电话，每项都给出原文依据。',
        channel: '网页聊天 / WorkBuddy',
      },
      {
        id: 'evidence-spreadsheet',
        title: '分析 Excel',
        prompt: '请用文件助手检查这份Excel，说明各工作表的主要内容和关键数据，并标明Sheet和单元格位置。',
        channel: '网页聊天 / WorkBuddy',
      },
    ],
  },
  {
    id: 'classification',
    title: '查看和更正分类',
    description: '可以浏览分类目录，也可以用完整文件名明确更正主分类。',
    items: [
      {
        id: 'classification-overview',
        title: '打开分类总览',
        prompt: '请用文件助手打开文件分类总览。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'classification-files',
        title: '查看分类下文件',
        prompt: '请用文件助手查看“学院/人事师资/人才工作”分类下的文件，并提供预览入口。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'classification-all-roles',
        title: '查看全部分类与角色',
        prompt: '请用文件助手查看“文件名.docx”的全部分类建议、分类角色和每项原文依据。',
        channel: 'WorkBuddy MCP',
        note: '系统会先精确搜索文件，再通过专用只读工具展示建议角色与正式生效角色；不会直接查询数据库。',
      },
      {
        id: 'classification-correct',
        title: '更正主分类',
        prompt: '请用文件助手将“01引进人才工作合同-王磊磊.doc”放入“学院/人事师资/人才工作”分类下。',
        channel: '网页聊天 / WorkBuddy',
        note: '请同时提供完整文件名和完整分类路径；明确更正不需要第二次确认。',
      },
      {
        id: 'classification-reclassify',
        title: '按当前规则重新分类',
        prompt: '请用文件助手重新分类“01引进人才工作合同-王磊磊.doc”，按当前分类规则重新生成建议；如果主分类改变，请同步整理工作副本，并逐项给出正文依据。',
        channel: '网页聊天 / WorkBuddy',
        note: '重新分类会复用已保存的解析内容，并在主分类确有变化时按本次明确请求更新工作副本位置；不修改受管原件。',
      },
      {
        id: 'classification-other',
        title: '查看“其他”文件',
        prompt: '请用文件助手查看“其他”分类中的文件。',
        channel: 'WorkBuddy MCP',
      },
    ],
  },
  {
    id: 'file-actions',
    title: '移动与重命名工作副本',
    description: '移动和重命名只作用于工作副本，不修改受管目录中的原始文件。',
    items: [
      {
        id: 'move-file',
        title: '明确移动',
        prompt: '请用文件助手将“文件名.docx”移动到受控目录“学院/人事师资/人才工作”。',
        channel: 'WorkBuddy MCP',
        note: '目标必须是受控目录；存在同名文件或路径冲突时会先提示处理。',
      },
      {
        id: 'rename-suggest',
        title: '只生成命名建议',
        prompt: '请用文件助手为刚才搜索到的文件生成规范名称，先不要执行。',
        channel: '网页聊天 / WorkBuddy',
      },
      {
        id: 'rename-file',
        title: '重命名文件',
        prompt: '请用文件助手把“旧文件名.docx”重命名为“2026年人才引进申请报告-王磊磊.docx”。',
        channel: '网页聊天 / WorkBuddy',
        note: '独立重命名会先生成操作计划，用户确认后才执行。',
      },
    ],
  },
  {
    id: 'directory-ingest',
    title: '导入授权目录',
    description: '使用管理员预先配置的授权根别名导入本地目录，不直接传服务器绝对路径。',
    items: [
      {
        id: 'directory-recursive',
        title: '递归导入目录',
        prompt: '请用文件助手递归导入授权根“test-materials”下的“人才引进/2026”目录，并自动归档和分类。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'directory-preserve-original',
        title: '保留原件并整理副本',
        prompt: '请用文件助手导入这个授权目录，保留原件，只整理工作副本，并逐文件报告分类结果。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'directory-progress',
        title: '查看导入进度',
        prompt: '请用文件助手查看刚才批量导入任务的处理进度。',
        channel: 'WorkBuddy MCP',
      },
    ],
  },
  {
    id: 'attachments',
    title: '处理当前聊天附件',
    description: 'WorkBuddy 必须已经安装附件桥接套件，并由当前轮提供可信附件提交单。',
    items: [
      {
        id: 'attachment-classify',
        title: '归档并分类附件',
        prompt: '请用文件助手归档并分类本轮附件，保留原件，并逐文件告诉我处理结果。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'attachment-ocr',
        title: 'OCR 扫描件',
        prompt: '请用文件助手OCR并归档刚上传的扫描件，告诉我哪些页面没有识别成功。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'attachment-duplicate',
        title: '检查重复附件',
        prompt: '请用文件助手检查本轮附件是否与已入库文件重复，并提供可点击的对比页面。',
        channel: 'WorkBuddy 附件套件',
      },
    ],
  },
  {
    id: 'duplicates',
    title: '处理重复文件',
    description: '先查看对比页面，再明确选择继续上传、使用已有文件或取消。',
    items: [
      {
        id: 'duplicate-open',
        title: '打开重复对比',
        prompt: '请用文件助手打开这个重复文件与已有文件的对比页面。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'duplicate-existing',
        title: '使用已有文件',
        prompt: '这个重复文件使用已有文件，不再上传新文件。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'duplicate-continue',
        title: '继续上传',
        prompt: '这个重复文件继续上传。',
        channel: 'WorkBuddy 附件套件',
      },
    ],
  },
  {
    id: 'status',
    title: '查询任务状态',
    description: '查看导入、分类、移动等异步任务的真实处理状态。',
    items: [
      {
        id: 'status-classification',
        title: '查看分类状态',
        prompt: '请用文件助手查看刚才的分类调整是否已经完成。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'status-batch',
        title: '查看批次结果',
        prompt: '请用文件助手列出刚才批次中成功、失败和等待处理的文件。',
        channel: 'WorkBuddy MCP',
      },
      {
        id: 'status-placement',
        title: '查看移动状态',
        prompt: '请用文件助手查看刚才文件移动操作的执行状态。',
        channel: 'WorkBuddy MCP',
      },
    ],
  },
];
