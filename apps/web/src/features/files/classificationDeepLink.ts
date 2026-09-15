// 分类页深链接只接受稳定分类 ID 和正整数页码，不信任显示名称或服务器路径。
export type ClassificationDeepLink = {
  categoryId: string | null;
  page: number;
  accessToken: string | null;
};

export function readClassificationDeepLink(search: string): ClassificationDeepLink {
  const params = new URLSearchParams(search);
  const rawCategoryId = (params.get('category_id') ?? '').trim();
  const rawPageText = params.get('page') ?? '1';
  const rawAccessToken = (params.get('classification_access_token') ?? '').trim();
  const rawPage = /^[1-9]\d*$/.test(rawPageText) ? Number(rawPageText) : 1;
  const categoryIsSafe = Boolean(
    rawCategoryId
    && rawCategoryId.length <= 200
    && !/[\u0000-\u001f/\\]/.test(rawCategoryId),
  );
  return {
    categoryId: categoryIsSafe ? rawCategoryId : null,
    page: Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage : 1,
    accessToken: rawAccessToken && rawAccessToken.length <= 512
      && !/[\u0000-\u001f]/.test(rawAccessToken)
      ? rawAccessToken
      : null,
  };
}

export function buildClassificationDeepLink(
  categoryId: string | null,
  page: number,
  accessToken: string | null = null,
): string {
  const params = new URLSearchParams();
  if (categoryId) params.set('category_id', categoryId);
  if (page > 1) params.set('page', String(page));
  if (accessToken) params.set('classification_access_token', accessToken);
  const query = params.toString();
  return query ? `/files?${query}` : '/files';
}
