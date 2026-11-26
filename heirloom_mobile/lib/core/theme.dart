import 'package:flutter/material.dart';

/// Exact CSS `:root` design tokens from `app/static/style.css` (Heirloom v2.6.6).
class AppColors {
  AppColors._();

  // Surfaces
  static const Color bg = Color(0xFF000000); // --bg
  static const Color surface = Color(0xFF222222); // --surface
  static const Color surfaceHover = Color(0xFF333333); // --surface-hover

  // Text
  static const Color text = Color(0xFFFFFFFF); // --text
  static const Color textMuted = Color(0xFFAAAAAA); // --text-muted

  // Brand
  static const Color accent = Color(0xFFE50914); // --accent
  static const Color accentHover = Color(0xFFB20710); // --accent-hover
  static const Color accentTransparent = Color.fromRGBO(229, 9, 20, 0.2); // --accent-transparent

  // Rating Gold
  static const Color ratingGold = Color(0xFFF5C518); // --rating-gold

  // Status
  static const Color success = Color(0xFF4CAF50); // --success
  static const Color failure = Color(0xFFE50914); // --failure

  // Password Strength (Weak, Good, Great)
  static const Color strengthWeak = Color(0xFFEF4444); // --strength-weak
  static const Color strengthGood = Color(0xFFF59E0B); // --strength-good
  static const Color strengthGreat = Color(0xFF22C55E); // --strength-great
  static const Color strengthTrack = Colors.transparent; // --strength-track

  // Overlays & Glass
  static const Color overlay = Color.fromRGBO(0, 0, 0, 0.8); // --overlay
  static const Color glass = Color.fromRGBO(40, 40, 40, 0.6); // --glass
  static const Color glassHover = Color.fromRGBO(255, 255, 255, 0.18); // --glass-hover
}

/// Exact CSS `--radius`, `--padding`, `--gap` shape tokens from `app/static/style.css`.
class AppSpacing {
  AppSpacing._();

  static const double radius = 6.0; // --radius: 6px
  static const double padding = 10.0; // --padding: 10px
  static const double gap = 6.0; // --gap: 6px
}

/// Button styles matching `.btn`, `.btn-secondary`, `.btn-danger` in `app/static/style.css`.
class AppButtonStyles {
  AppButtonStyles._();

  static final ButtonStyle secondary = ElevatedButton.styleFrom(
    backgroundColor: AppColors.surface,
    foregroundColor: AppColors.text,
    elevation: 0,
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSpacing.radius)),
  );

  static final ButtonStyle danger = ElevatedButton.styleFrom(
    backgroundColor: AppColors.accentTransparent,
    foregroundColor: AppColors.accent,
    elevation: 0,
    side: const BorderSide(color: AppColors.accent, width: 1),
    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSpacing.radius)),
  );
}

class AppTheme {
  static const String appVersion = '2.6.6';

  // Web-aligned token aliases
  static const Color primaryRed = AppColors.accent;
  static const Color primaryRedHover = AppColors.accentHover;
  static const Color accentPrimary = AppColors.accent;
  static const Color accentHover = AppColors.accentHover;

  static const Color bgPrimary = AppColors.bg;
  static const Color bgSecondary = AppColors.surface;
  static const Color bgTertiary = AppColors.surface;

  static const Color textPrimary = AppColors.text;
  static const Color textSecondary = AppColors.textMuted;
  static const Color textTertiary = AppColors.textMuted;

  static final BorderRadius defaultRadius = BorderRadius.circular(AppSpacing.radius);

  static final ThemeData darkTheme = ThemeData(
    useMaterial3: true,
    brightness: Brightness.dark,
    scaffoldBackgroundColor: AppColors.bg,
    primaryColor: AppColors.accent,
    dividerColor: AppColors.surfaceHover,
    colorScheme: const ColorScheme.dark(
      primary: AppColors.accent,
      secondary: AppColors.accentHover,
      surface: AppColors.surface,
      onPrimary: AppColors.text,
      onSecondary: AppColors.text,
      onSurface: AppColors.text,
      error: AppColors.failure,
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: AppColors.bg,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      centerTitle: false,
      iconTheme: IconThemeData(color: AppColors.textMuted),
      titleTextStyle: TextStyle(
        color: AppColors.text,
        fontSize: 24, // 1.5rem matching web header h1
        fontWeight: FontWeight.bold,
      ),
    ),
    cardTheme: CardTheme(
      color: AppColors.surface,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
        side: BorderSide.none,
      ),
      margin: EdgeInsets.zero,
    ),
    dialogTheme: DialogTheme(
      backgroundColor: AppColors.surface,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
      ),
    ),
    bottomSheetTheme: const BottomSheetThemeData(
      backgroundColor: AppColors.surface,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(12)),
      ),
    ),
    bottomNavigationBarTheme: const BottomNavigationBarThemeData(
      backgroundColor: AppColors.bg,
      selectedItemColor: AppColors.accent,
      unselectedItemColor: AppColors.textMuted,
      type: BottomNavigationBarType.fixed,
      elevation: 0,
    ),
    textTheme: const TextTheme(
      displayLarge: TextStyle(color: AppColors.text, fontSize: 32, fontWeight: FontWeight.bold),
      titleLarge: TextStyle(color: AppColors.text, fontSize: 20, fontWeight: FontWeight.bold),
      bodyLarge: TextStyle(color: AppColors.text, fontSize: 16),
      bodyMedium: TextStyle(color: AppColors.textMuted, fontSize: 14),
      bodySmall: TextStyle(color: AppColors.textMuted, fontSize: 12),
    ),
    elevatedButtonTheme: ElevatedButtonThemeData(
      style: ElevatedButton.styleFrom(
        backgroundColor: AppColors.accent,
        foregroundColor: AppColors.text,
        elevation: 0,
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppSpacing.radius),
          side: BorderSide.none,
        ),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        backgroundColor: AppColors.surface,
        foregroundColor: AppColors.text,
        side: BorderSide.none,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppSpacing.radius),
        ),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: AppColors.textMuted,
      ),
    ),
    checkboxTheme: CheckboxThemeData(
      fillColor: WidgetStateProperty.resolveWith((states) {
        if (states.contains(WidgetState.selected)) return AppColors.accent;
        return AppColors.surfaceHover;
      }),
      checkColor: WidgetStateProperty.all(AppColors.text),
      side: BorderSide.none,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(4)),
    ),
    chipTheme: ChipThemeData(
      backgroundColor: AppColors.surfaceHover,
      selectedColor: AppColors.accent,
      secondarySelectedColor: AppColors.accent,
      showCheckmark: false,
      labelStyle: const TextStyle(color: AppColors.text, fontSize: 12, fontWeight: FontWeight.bold),
      secondaryLabelStyle: const TextStyle(color: AppColors.text, fontSize: 12, fontWeight: FontWeight.bold),
      side: BorderSide.none,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
        side: BorderSide.none,
      ),
    ),
    dividerTheme: const DividerThemeData(
      color: AppColors.surfaceHover,
      thickness: 1,
    ),
    // Matches web `input, select { border: none; outline: none; background: var(--surface); }`
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: AppColors.surface,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
        borderSide: BorderSide.none,
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
        borderSide: BorderSide.none,
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
        borderSide: BorderSide.none,
      ),
      errorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
        borderSide: BorderSide.none,
      ),
      focusedErrorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppSpacing.radius),
        borderSide: BorderSide.none,
      ),
      hintStyle: const TextStyle(color: AppColors.textMuted),
      labelStyle: const TextStyle(color: AppColors.textMuted),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
    ),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: AppColors.surface,
      contentTextStyle: const TextStyle(color: AppColors.text),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSpacing.radius)),
      behavior: SnackBarBehavior.floating,
    ),
  );
}
