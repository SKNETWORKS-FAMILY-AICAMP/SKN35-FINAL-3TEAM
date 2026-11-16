from contracts.permission import PermissionResult, Decision


class MathPermissionChecker:
    def check(self, call):

        # 관리자는 모든 Tool 사용 가능
        if "admin" in call.principal.roles:
            return PermissionResult(
                Decision.ALLOW
            )

        # 일반 사용자는 더하기만 가능
        if call.tool_name == "add":
            return PermissionResult(
                Decision.ALLOW
            )

        # 나머지는 거부
        return PermissionResult(
            Decision.DENY,
            reason="이 Tool을 사용할 권한이 없습니다."
        )