import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import '../api_client.dart';
import '../theme.dart';
import '../../features/auth/profile_picker_screen.dart';

class ProfileAvatarButton extends ConsumerWidget {
  const ProfileAvatarButton({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final activeProfileAsync = ref.watch(activeProfileInfoProvider);

    return activeProfileAsync.when(
      loading: () => const Padding(
        padding: EdgeInsets.symmetric(horizontal: 12.0),
        child: Center(
          child: SizedBox(
            width: 28,
            height: 28,
            child: CircularProgressIndicator(strokeWidth: 2, color: AppTheme.primaryRed),
          ),
        ),
      ),
      error: (_, __) => IconButton(
        icon: const Icon(LucideIcons.user),
        onPressed: () => context.push('/profiles'),
      ),
      data: (profile) {
        final color = parseProfileColor(profile?['avatar_color']);
        final icon = getProfileIcon(profile?['avatar_icon']);

        return Padding(
          padding: const EdgeInsets.only(right: 8.0),
          child: GestureDetector(
            onTap: () => _showProfileMenuSheet(context, ref, profile),
            child: CircleAvatar(
              radius: 17,
              backgroundColor: color,
              child: Icon(icon, size: 18, color: Colors.white),
            ),
          ),
        );
      },
    );
  }

  void _showProfileMenuSheet(BuildContext context, WidgetRef ref, Map<String, dynamic>? profile) {
    final name = profile?['name'] as String? ?? 'Profile';
    final isPrimary = profile?['is_primary'] as bool? ?? false;
    final ratingLevel = profile?['rating_level'] as int? ?? 0;
    final isKids = profile?['is_kids'] as bool? ?? false;
    final color = parseProfileColor(profile?['avatar_color']);
    final icon = getProfileIcon(profile?['avatar_icon']);

    showModalBottomSheet(
      context: context,
      backgroundColor: Colors.transparent,
      builder: (ctx) => Container(
        decoration: const BoxDecoration(
          color: AppTheme.bgSecondary,
          borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
        ),
        padding: const EdgeInsets.symmetric(vertical: 20, horizontal: 16),
        child: SafeArea(
          top: false,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              // Drag handle
              Container(
                width: 36,
                height: 4,
                decoration: BoxDecoration(
                  color: Colors.white24,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 16),

              // Active Profile Details Row
              Row(
                children: [
                  CircleAvatar(
                    backgroundColor: color,
                    radius: 26,
                    child: Icon(icon, size: 28, color: Colors.white),
                  ),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          name,
                          style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: Colors.white),
                        ),
                        const SizedBox(height: 4),
                        if (isPrimary)
                          _buildTag('Household Owner', const Color(0xFFF5C518))
                        else if (ratingLevel == 2 || isKids)
                          _buildTag('Child Profile', const Color(0xFF4ADE80))
                        else if (ratingLevel == 1)
                          _buildTag('Teen Profile', const Color(0xFF60A5FA))
                        else
                          _buildTag('Adult Profile', Colors.white70),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 16),
              const Divider(color: Colors.white12),

              // Menu Items
              ListTile(
                leading: const Icon(LucideIcons.users, color: Colors.white),
                title: const Text('Switch Profiles', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
                trailing: const Icon(LucideIcons.chevronRight, size: 16, color: AppTheme.textTertiary),
                onTap: () {
                  Navigator.of(ctx).pop();
                  context.push('/profiles');
                },
              ),
              ListTile(
                leading: const Icon(LucideIcons.settings, color: Colors.white),
                title: const Text('Settings', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
                trailing: const Icon(LucideIcons.chevronRight, size: 16, color: AppTheme.textTertiary),
                onTap: () {
                  Navigator.of(ctx).pop();
                  context.push('/settings');
                },
              ),
              ListTile(
                leading: const Icon(LucideIcons.logOut, color: AppTheme.primaryRed),
                title: const Text('Sign Out', style: TextStyle(color: AppTheme.primaryRed, fontWeight: FontWeight.w600)),
                onTap: () async {
                  Navigator.of(ctx).pop();
                  final prefs = ref.read(sharedPreferencesProvider);
                  await prefs.remove('auth_token');
                  await prefs.remove('active_profile_id');
                  ref.read(authTokenProvider.notifier).state = null;
                  ref.read(activeProfileIdProvider.notifier).state = null;
                  if (context.mounted) {
                    context.go('/login');
                  }
                },
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildTag(String text, Color color) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: color.withOpacity(0.4)),
      ),
      child: Text(
        text,
        style: TextStyle(color: color, fontSize: 11, fontWeight: FontWeight.bold),
      ),
    );
  }
}

