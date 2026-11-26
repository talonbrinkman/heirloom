import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

final inboxDataProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final resp = await dio.get('/api/inbox');
  return Map<String, dynamic>.from(resp.data as Map);
});

class InboxScreen extends ConsumerWidget {
  const InboxScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final inboxAsync = ref.watch(inboxDataProvider);

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
        title: const Text('Inbox'),
        actions: [
          IconButton(
            icon: const Icon(LucideIcons.refreshCw),
            onPressed: () => ref.invalidate(inboxDataProvider),
          ),
        ],
      ),
      body: inboxAsync.when(
        loading: () => const Center(child: CircularProgressIndicator(color: AppColors.accent)),
        error: (err, _) => Center(child: Text(formatApiError(err, fallback: 'Unable to load inbox.'), textAlign: TextAlign.center, style: const TextStyle(color: AppColors.failure))),
        data: (data) {
          final invites = (data['invites'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
          final resets = (data['reset_requests'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
          final downloads = (data['loom_downloads'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
          final uploads = (data['loom_uploads'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();

          if (invites.isEmpty && resets.isEmpty && downloads.isEmpty && uploads.isEmpty) {
            return const Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(LucideIcons.inbox, size: 48, color: AppColors.textMuted),
                  SizedBox(height: 12),
                  Text('Your inbox is empty', style: TextStyle(color: AppColors.textMuted)),
                ],
              ),
            );
          }

          return RefreshIndicator(
            onRefresh: () => ref.refresh(inboxDataProvider.future),
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                if (invites.isNotEmpty) ...[
                  const Text('Watch Together Invites', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 8),
                  ...invites.map((inv) => Container(
                        margin: const EdgeInsets.only(bottom: 10),
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: AppColors.surface,
                          borderRadius: BorderRadius.circular(AppSpacing.radius),
                        ),
                        child: Row(
                          children: [
                            const Icon(LucideIcons.users, color: AppColors.accent),
                            const SizedBox(width: 12),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text('${inv['sender']} invited you', style: const TextStyle(fontWeight: FontWeight.bold)),
                                  Text('${inv['title']} (${inv['media_type']})', style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
                                ],
                              ),
                            ),
                            TextButton(
                              onPressed: () async {
                                await ref.read(apiClientProvider).post('/watch_together/decline/${inv['id']}');
                                ref.invalidate(inboxDataProvider);
                              },
                              child: const Text('Decline', style: TextStyle(color: AppColors.textMuted)),
                            ),
                            ElevatedButton(
                              onPressed: () async {
                                final resp = await ref.read(apiClientProvider).post('/watch_together/accept/${inv['id']}');
                                ref.invalidate(inboxDataProvider);
                                if (context.mounted && resp.data['success'] == true) {
                                  ScaffoldMessenger.of(context).showSnackBar(
                                    SnackBar(content: Text('Joined session ${resp.data['session_id']}')),
                                  );
                                }
                              },
                              child: const Text('Join'),
                            ),
                          ],
                        ),
                      )),
                  const SizedBox(height: 16),
                ],
                if (resets.isNotEmpty) ...[
                  const Text('Password Reset Requests', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 8),
                  ...resets.map((r) => ListTile(
                        tileColor: AppColors.surface,
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSpacing.radius)),
                        leading: const Icon(LucideIcons.keyRound, color: AppColors.ratingGold),
                        title: Text('Reset request from ${r['username']}'),
                        subtitle: Text(r['created_at']?.toString() ?? '', style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
                      )),
                  const SizedBox(height: 16),
                ],
                if (downloads.isNotEmpty || uploads.isNotEmpty) ...[
                  const Text('Active Loom Transfers', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 8),
                  ...downloads.map((d) => Container(
                        margin: const EdgeInsets.only(bottom: 8),
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: AppColors.surface,
                          borderRadius: BorderRadius.circular(AppSpacing.radius),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Row(
                              mainAxisAlignment: MainAxisAlignment.spaceBetween,
                              children: [
                                Expanded(
                                  child: Text(
                                    '${d['title']} (${d['peer_name'] ?? 'Peer'})',
                                    style: const TextStyle(fontWeight: FontWeight.w600),
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                                Text('${d['progress_pct'] ?? 0}%', style: const TextStyle(color: AppColors.accent, fontSize: 12)),
                              ],
                            ),
                            const SizedBox(height: 6),
                            LinearProgressIndicator(
                              value: (((d['progress_pct'] as num?) ?? 0) / 100.0).clamp(0.0, 1.0),
                              color: AppColors.accent,
                              backgroundColor: AppColors.surfaceHover,
                            ),
                          ],
                        ),
                      )),
                  ...uploads.map((u) => ListTile(
                        tileColor: AppColors.surface,
                        leading: const Icon(LucideIcons.upload, color: AppColors.success),
                        title: Text(u['title']?.toString() ?? 'Loom Upload'),
                        subtitle: Text('To peer: ${u['peer_name'] ?? ''}', style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
                      )),
                ],
              ],
            ),
          );
        },
      ),
    );
  }
}
