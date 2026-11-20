export async function request(user, path, { method = 'GET', body, signal } = {}) {
  let response;
  try {
    response = await fetch('/api' + path, {
      method, signal, headers: {
        'X-User-Id': user,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('API에 연결할 수 없습니다. FastAPI 서버를 확인해 주세요.');
  }

  if (response.status === 204) return;
  const data = await response.json().catch(() => null);
  if (!response.ok || data === null) {
    const detail = data?.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map(item => item.msg).join(', ')
      : 'API 응답을 읽을 수 없습니다. FastAPI 서버를 확인해 주세요.';
    throw Object.assign(new Error(message), { status: response.status });
  }
  return data;
}
