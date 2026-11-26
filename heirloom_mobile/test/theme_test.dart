import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:heirloom_mobile/core/theme.dart';

void main() {
  test('AppColors match exact web style.css :root CSS variable values', () {
    expect(AppColors.bg, const Color(0xFF000000));
    expect(AppColors.surface, const Color(0xFF222222));
    expect(AppColors.surfaceHover, const Color(0xFF333333));
    expect(AppColors.text, const Color(0xFFFFFFFF));
    expect(AppColors.textMuted, const Color(0xFFAAAAAA));
    expect(AppColors.accent, const Color(0xFFE50914));
    expect(AppColors.accentHover, const Color(0xFFB20710));
    expect(AppColors.ratingGold, const Color(0xFFF5C518));
    expect(AppColors.success, const Color(0xFF4CAF50));
    expect(AppColors.failure, const Color(0xFFE50914));
    expect(AppColors.strengthWeak, const Color(0xFFEF4444));
    expect(AppColors.strengthGood, const Color(0xFFF59E0B));
    expect(AppColors.strengthGreat, const Color(0xFF22C55E));
    expect(AppSpacing.radius, 6.0);
    expect(AppSpacing.padding, 10.0);
    expect(AppSpacing.gap, 6.0);
    expect(AppTheme.appVersion, '2.6.6');
  });
}
