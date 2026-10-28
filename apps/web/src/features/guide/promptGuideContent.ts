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
      {
        id: 'extraction-preview-before-save',
        title: '提取后先核对',
        prompt: '请先读取本轮上传文件的正文和表格，逐项展示识别结果及原文位置；先不要保存，等我核对。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'extraction-confirm-save',
        title: '确认提取结果并保存',
        prompt: '我确认刚才展示的提取结果无误，请保存到文件助手。',
        channel: 'WorkBuddy 附件套件',
        note: '仅用于同一轮已经展示完整提取结果并等待确认的文件。',
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
      {
        id: 'attachment-retry-failed',
        title: '重试导入失败的文件',
        prompt: '请重试刚才导入失败的《文件完整名》，并告诉我最终结果。',
        channel: 'WorkBuddy 附件套件',
        note: '仅适用于本轮确实导入失败的文件。',
      },
      {
        id: 'attachment-cancel-unfinished',
        title: '取消未完成的导入',
        prompt: '请取消刚才尚未完成导入的《文件完整名》，保留其他已完成文件。',
        channel: 'WorkBuddy 附件套件',
      },
    ],
  },
  {
    id: 'teacher-recruitment-excel',
    title: '教师引进汇总表',
    description: '上传汇总表时以本轮上传的表为准；未上传汇总表时按完整文件名追加。补充已有内容前先获取当前版本。',
    items: [
      {
        id: 'teacher-recruitment-new-target',
        title: '上传汇总表填表',
        prompt: '请把本轮上传的教师引进材料填入同时上传的汇总表，缺失信息留空，保存并给我下载链接。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'teacher-recruitment-append',
        title: '按文件名追加',
        prompt: '请把本轮上传的教师引进材料追加到已发布的《汇总表完整文件名》，保存并给我新增行号和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '使用前把书名号内的文字替换为含扩展名的完整文件名。',
      },
      {
        id: 'teacher-recruitment-checkout',
        title: '下载后补充',
        prompt: '请给我《汇总表完整文件名》的当前版本，我要补充已有单元格；请记录回传所需的版本信息。',
        channel: 'WorkBuddy 附件套件',
        note: '先下载并记录编辑前版本，再修改和保存。',
      },
      {
        id: 'teacher-recruitment-return',
        title: '补充后回传',
        prompt: '请用编辑前记录的版本信息，把本轮上传的补充版《汇总表完整文件名》发布为同一张表的新版本，并给我下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '上传文件须与目标表完全同名；只补充已有人员行，不新增或删除人员。',
      },
    ],
  },
  {
    id: 'research-funding-balance',
    title: '教师成果资助余额表',
    description: '可首次生成、从图片追加、通过文字新增修改删除登记、更新资助金额，或在下载修改后回传。',
    items: [
      {
        id: 'funding-balance-create',
        title: '首次生成余额表',
        prompt: '请根据本轮上传的成果资助使用登记图片和教师成果明细汇总表，读取各教师的资助总额，只从手写使用登记中提取支出金额，生成新的教师成果资助使用登记余额表。首页保留来源表中的全部教师，无法核对的金额标明原因，给我新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '首次使用时同时上传登记图片和包含全部教师的成果明细汇总表。',
      },
      {
        id: 'funding-single-teacher-and-balance',
        title: '单人资助表更新两张表',
        prompt: '请读取我上传的单人科研成果资助表第 1 张论文页和第 3 张会议页，将新成果追加到《教师成果明细汇总表.xlsx》对应教师的个人页并更新“汇总”页。该表正式发布后，用新版本更新《教师成果资助使用登记余额表.xlsx》的科研成果资助金额，按已有登记重算支出和余额。请分别给出两张表的新版本、下载链接和待核对事项。',
        channel: 'WorkBuddy 附件套件',
        note: '请上传单人资助表，并把两张目标表的文件名替换为服务器上实际的完整文件名；缺申请人的记录须先确认归属。',
      },
      {
        id: 'funding-balance-and-source-summary',
        title: '同步来源表和余额表',
        prompt: '请根据我上传的教师成果明细汇总表，以各教师个人明细页核对无误的资助金额为准，同步该表的“汇总”页；有变化时发布同名新版本。再按完整文件名找到《教师成果资助使用登记余额表.xlsx》，根据已有使用登记重新计算总支出和余额，同步首页与个人明细页并发布新版本。请给出两份表的新版本和下载链接；无法核对的金额请标明并保留原值。',
        channel: 'WorkBuddy 附件套件',
        note: '请上传已保存的成果明细汇总表，并将余额表文件名替换为实际完整文件名。',
      },
      {
        id: 'funding-balance-only',
        title: '只更新余额表',
        prompt: '请根据我上传的教师成果明细汇总表，按个人明细页核对无误的资助金额更新《教师成果资助使用登记余额表.xlsx》。总支出按余额表已有的使用登记重新计算，同步首页与个人明细页，只发布余额表的新版本，不修改上传的成果明细表。无法核对的金额请标明并保留原值，给出余额表的新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '请上传已保存的成果明细汇总表，并将余额表文件名替换为实际完整文件名。',
      },
      {
        id: 'funding-balance-append',
        title: '追加新使用登记',
        prompt: '请读取我本轮上传的成果资助使用登记图片，把新的手写使用登记追加到已发布的《教师成果资助使用登记余额表.xlsx》。按手写登记中的金额计算支出，更新对应教师的个人明细页和首页余额，并发布同名新版本。请告诉我新增记录、待核对事项、新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '请上传新的登记图片，并将余额表文件名替换为实际完整文件名。',
      },
      {
        id: 'funding-balance-prompt-add',
        title: '用文字新增一笔登记',
        prompt: '请在已发布的《教师成果资助使用登记余额表.xlsx》中，给张三新增一笔使用登记：2026年10月26日会议支出100元。请先核对当前明细，再新增这一笔，重算个人页与首页金额，发布同名新版本并给我下载链接。',
        channel: 'WorkBuddy MCP',
        note: '替换为真实文件名、姓名、登记内容、日期和金额；不需要上传图片。',
      },
      {
        id: 'funding-balance-prompt-update',
        title: '用文字修改登记或资助金额',
        prompt: '请把已发布的《教师成果资助使用登记余额表.xlsx》中张三的“2026年10月26日会议支出100元”改为120元，并把他的其他资助金额改为500元。请核对具体登记后更新个人页与首页，重新计算余额，发布同名新版本并给我下载链接。',
        channel: 'WorkBuddy MCP',
        note: '登记不唯一时需要先指定具体一笔；修改个人页其他资助金额后，首页会同步更新。',
      },
      {
        id: 'funding-balance-prompt-delete',
        title: '用文字删除一笔登记',
        prompt: '请从已发布的《教师成果资助使用登记余额表.xlsx》中删除张三的“2026年10月26日会议支出100元”这一笔登记。请先核对具体记录，再重新计算个人页与首页的支出和余额，发布同名新版本并给我下载链接。',
        channel: 'WorkBuddy MCP',
        note: '只删除明确指定的登记，保留其他记录和历史版本。',
      },
      {
        id: 'funding-balance-prompt-rename',
        title: '更正教师姓名',
        prompt: '请用文件助手把已发布的《教师成果资助使用登记余额表.xlsx》中教师“张叁”的姓名更正为“张三”。请先核对当前教师名单，保持金额不变，同步更新首页、个人明细页和该教师的登记归属，发布同名新版本并给我下载链接。',
        channel: 'WorkBuddy MCP',
        note: '请替换为真实的旧姓名和正确姓名；新姓名不能与表中其他教师重名。',
      },
      {
        id: 'funding-balance-return',
        title: '核对修改后回传',
        prompt: '我已在下载的《教师成果资助使用登记余额表.xlsx》个人明细页核对并修改信息。请用编辑前记录的版本信息，将本轮上传的同名文件回传并发布新版本；以个人明细页为准重新编号、计算支出和余额，同步更新首页。请给出变更结果、新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '请先下载并记录编辑前版本；修改后保存、关闭文件，再上传与目标完全同名的表格。',
      },
      {
        id: 'funding-balance-checkout',
        title: '下载当前版本供核对',
        prompt: '请给我《教师成果资助使用登记余额表.xlsx》的当前版本供我核对修改，并记录回传时需要使用的编辑前版本信息。',
        channel: 'WorkBuddy 附件套件',
        note: '修改前先执行，文件名请替换为实际完整文件名。',
      },
    ],
  },
  {
    id: 'professor-performance-excel',
    title: '教授业绩汇总表',
    description: '上传模板首次填表，或按完整文件名追加职称评审材料；仅填写身份任职信息和考核优秀年度。',
    items: [
      {
        id: 'professor-performance-new-target',
        title: '上传模板首次填表',
        prompt: '请把本轮上传的职称评审材料填入同时上传的教授业绩汇总表，只填写 B–I 列和 L 列；无法确认的内容留空。每份材料一行，保存并给我新增行号、新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '同时上传目标表和一至二十份评审材料；材料可为 PDF 或 Word。',
      },
      {
        id: 'professor-performance-append',
        title: '按文件名追加材料',
        prompt: '请把本轮上传的职称评审材料追加到已发布的《申报人员业绩汇总-教学科研型教授.xlsx》，只填写 B–I 列和 L 列；无法确认的内容留空。请给我新增行号、新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '只上传新材料，并将目标表名称替换为实际完整文件名。',
      },
      {
        id: 'professor-performance-checkout',
        title: '下载后补充',
        prompt: '请给我《申报人员业绩汇总-教学科研型教授.xlsx》的当前版本供我补充，并记录回传时需要使用的编辑前版本信息。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'professor-performance-return',
        title: '补充后回传',
        prompt: '请用编辑前记录的版本信息，把本轮上传的同名补充版《申报人员业绩汇总-教学科研型教授.xlsx》回传并发布为新版本，给我下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '请先下载当前版本，补充后保存、关闭文件，再上传与目标完全同名的表格。',
      },
    ],
  },
  {
    id: 'general-excel-writeback',
    title: '按表头填写和修正表格',
    description: '按真实表头填表、按完整文件名追加，或下载修改后回传同一张表。',
    items: [
      {
        id: 'general-excel-new-target',
        title: '上传目标表填表',
        prompt: '请根据本轮上传的材料，按同时上传的目标表的真实表头填写可核实的信息，缺失内容留空，保存并给我新增行号、新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'general-excel-doctoral-target',
        title: '填写博士应聘统计表',
        prompt: '请把本轮上传的博士应聘材料填入同时上传的统计表，按实际表头和列位置填写，保留空白列；多篇论文在同一单元格逐篇编号并换行。无法确认的信息留空，保存并给我新增行号、新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '请同时上传材料和目标表；以本轮上传的表为准。',
      },
      {
        id: 'general-excel-append',
        title: '按文件名追加',
        prompt: '请把本轮上传的材料追加到已发布的《目标表完整文件名》，按当前表头填写可核实的信息；同一列有多条信息时逐条编号并换行，空白列保持空白。保存并给我新增行号、新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '把书名号内文字替换为含扩展名的完整文件名。',
      },
      {
        id: 'general-excel-correct',
        title: '修正已发布记录',
        prompt: '请根据本轮上传的证明材料，把已发布的《目标表完整文件名》第五行“字段名”的错误内容更正为“正确内容”，核对来源后发布同名新版本，并列出修改前后内容。',
        channel: 'WorkBuddy 附件套件',
        note: '使用前替换文件名、真实行号、字段和正确内容；更正已有行不会新增一行。',
      },
      {
        id: 'general-excel-checkout',
        title: '下载后补充',
        prompt: '请给我《目标表完整文件名》的当前版本供我补充，并记录回传所需的编辑前版本信息。',
        channel: 'WorkBuddy 附件套件',
      },
      {
        id: 'general-excel-return',
        title: '补充后回传',
        prompt: '我已修改下载的《目标表完整文件名》。请使用编辑前记录的版本信息，将本轮上传的同名表回传并发布为新版本，给我新版本和下载链接。',
        channel: 'WorkBuddy 附件套件',
        note: '先下载当前版本并记录版本信息；修改已有数据格后保存、关闭，再上传完全同名的表格。新增人员请使用材料追加流程。',
      },
    ],
  },
  {
    id: 'journal-catalog',
    title: '查询学术期刊等级',
    description: '按系统保存的二〇一九版目录查询期刊，不需要重新上传目录。',
    items: [
      {
        id: 'journal-catalog-lookup',
        title: '查询期刊目录',
        prompt: '请按系统保存的二〇一九版学术期刊目录查询《期刊完整名称》的等级，给出命中的目录条目和依据；找不到或有歧义时请说明。',
        channel: 'WorkBuddy MCP',
        note: '将期刊完整名称替换为实际名称；有刊号时也可一并提供。',
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
      {
        id: 'duplicate-cancel',
        title: '取消重复上传',
        prompt: '请取消本轮重复文件《文件完整名》的上传。',
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
