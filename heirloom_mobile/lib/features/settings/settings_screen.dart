import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

final userProfileProvider = FutureProvider<Map<String, dynamic>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/me');
  return response.data as Map<String, dynamic>;
});

class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final userProfileAsync = ref.watch(userProfileProvider);
    
    return Scaffold(
      appBar: AppBar(
        title: const Text('Settings'),
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
      ),
      body: ListView(
        children: [
          const SizedBox(height: 16),
          ListTile(
            leading: const Icon(LucideIcons.server, color: AppTheme.textSecondary),
            title: const Text('Server Connection'),
            subtitle: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(ref.watch(serverUrlProvider) ?? 'Not connected'),
                userProfileAsync.when(
                  data: (data) => Text('Logged in as: ${data['username']}', style: const TextStyle(color: AppTheme.primaryRed)),
                  loading: () => const Text('Loading user...'),
                  error: (err, stack) => const Text('Failed to load user'),
                ),
              ],
            ),
            trailing: const Icon(LucideIcons.chevronRight, color: AppTheme.textSecondary),
            onTap: () {
              // Usually we'd let them edit it, but for now just show it
            },
          ),
          const Divider(color: AppTheme.bgTertiary),
          ListTile(
            leading: const Icon(LucideIcons.logOut, color: AppTheme.primaryRed),
            title: const Text('Log Out', style: TextStyle(color: AppTheme.primaryRed)),
            onTap: () async {
              // Clear token
              ref.read(authTokenProvider.notifier).state = null;
              final prefs = ref.read(sharedPreferencesProvider);
              await prefs.remove('auth_token');
              if (context.mounted) {
                context.go('/login');
              }
            },
          ),
        ],
      ),
    );
  }
}
