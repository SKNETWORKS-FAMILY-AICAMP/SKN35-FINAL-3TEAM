export function failedToolSummary(event, denial) {
  if (denial?.call_id === event.call_id) {
    return {
      rejected: '승인을 거절해 실행하지 않음',
      timeout: '승인 시간이 지나 실행하지 않음',
      deny: '권한이 없어 실행하지 않음',
    }[denial.kind] ?? '실행하지 않음';
  }
  const reason = event.result.replace(/^error:\s*/, '');
  if (/not executed|실행하지 않았/.test(reason)) return '앞선 작업이 중단되어 실행하지 않음';
  if (reason.startsWith('permission not approved (user rejected)')) return '승인을 거절해 실행하지 않음';
  if (reason.startsWith('permission not approved (approval timed out)')) return '승인 시간이 지나 실행하지 않음';
  if (reason.startsWith('permission denied')) return '권한이 없어 실행하지 않음';
  return reason.length > 60 ? reason.slice(0, 60) + '…' : reason;
}
