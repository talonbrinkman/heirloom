import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:url_launcher/url_launcher.dart';
import '../api_client.dart';
import '../theme.dart';

/// Unified 7-item media action sheet matching the Heirloom web v2.6.6 three-dots menu:
/// 1. Mark as Watched / Unwatched
/// 2. Remove Watch Progress (when in progress or watched)
/// 3. Watch Together
/// 4. Download
/// 5. Add to / Remove from Watchlist
/// 6. Refresh Metadata (Admin)
/// 7. Fix Match (Admin)
Future<void> showMediaActionSheet({
  required BuildContext context,
  required String mediaType,
  required int itemId,
  required String title,
  String? year,
  bool isWatched = false,
  bool hasProgress = false,
  bool inWatchlist = false,
  String? historyScope,
  VoidCallback? onMutated,
}) {
  return showModalBottomSheet(
    context: context,
    backgroundColor: AppColors.surface,
    isScrollControlled: true,
    builder: (ctx) => _MediaActionSheetBody(
      mediaType: mediaType,
      itemId: itemId,
      title: title,
      year: year,
      isWatched: isWatched,
      hasProgress: hasProgress,
      inWatchlist: inWatchlist,
      historyScope: historyScope,
      onMutated: onMutated,
    ),
  );
}

class _MediaActionSheetBody extends ConsumerStatefulWidget {
  final String mediaType;
  final int itemId;
  final String title;
  final String? year;
  final bool isWatched;
  final bool hasProgress;
  final bool inWatchlist;
  final String? historyScope;
  final VoidCallback? onMutated;

  const _MediaActionSheetBody({
    required this.mediaType,
    required this.itemId,
    required this.title,
    this.year,
    required this.isWatched,
    required this.hasProgress,
    required this.inWatchlist,
    this.historyScope,
    this.onMutated,
  });

  @override
  ConsumerState<_MediaActionSheetBody> createState() => _MediaActionSheetBodyState();
}

class _MediaActionSheetBodyState extends ConsumerState<_MediaActionSheetBody> {
  bool _busy = false;

  void _snack(String msg, {bool isError = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg),
        backgroundColor: isError ? AppColors.failure : AppColors.surface,
      ),
    );
  }

  Future<void> _runMutation(Future<void> Function() fn, String successMsg) async {
    setState(() => _busy = true);
    try {
      await fn();
      widget.onMutated?.call();
      if (mounted) {
        Navigator.of(context).pop();
        _snack(successMsg);
      }
    } catch (e) {
      if (mounted) {
        setState(() => _busy = false);
        _snack(formatApiError(e), isError: true);
      }
    }
  }

  Future<void> _toggleWatched() async {
    final dio = ref.read(apiClientProvider);
    final endpoint = widget.isWatched
        ? '/history/mark_unwatched/${widget.mediaType}/${widget.itemId}'
        : '/history/mark_watched/${widget.mediaType}/${widget.itemId}';
    final scope = widget.mediaType == 'tv' ? (widget.historyScope ?? 'series') : null;
    await _runMutation(
      () => dio.post(endpoint, queryParameters: scope != null ? {'scope': scope} : null),
      widget.isWatched ? 'Marked as unwatched' : 'Marked as watched',
    );
  }

  Future<void> _removeWatchProgress() async {
    final dio = ref.read(apiClientProvider);
    final scope = widget.mediaType == 'tv' ? (widget.historyScope ?? 'series') : null;
    await _runMutation(
      () => dio.post(
        '/history/remove/${widget.mediaType}/${widget.itemId}',
        queryParameters: scope != null ? {'scope': scope} : null,
      ),
      'Watch progress removed',
    );
  }

  Future<void> _toggleWatchlist() async {
    final dio = ref.read(apiClientProvider);
    await _runMutation(
      () => dio.post('/watchlist/toggle', data: {
        'media_type': widget.mediaType,
        'item_id': widget.itemId,
      }),
      widget.inWatchlist ? 'Removed from watchlist' : 'Added to watchlist',
    );
  }

  Future<void> _downloadMedia() async {
    final session = ref.read(appSessionProvider);
    final url = '${session.serverUrl}/download/${widget.mediaType}/${widget.itemId}?token=${session.authToken}';
    final uri = Uri.parse(url);
    try {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Could not launch download.'), isError: true);
    }
  }

  Future<void> _startWatchTogether() async {
    final dio = ref.read(apiClientProvider);
    setState(() => _busy = true);
    try {
      final eligibleResp = await dio.get(
        '/api/users/eligible',
        queryParameters: {'media_type': widget.mediaType},
      );
      final users = (eligibleResp.data as List? ?? [])
          .whereType<Map>()
          .map((e) => Map<String, dynamic>.from(e))
          .toList();

      if (!mounted) return;
      setState(() => _busy = false);

      final selected = <String>{};
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (dCtx) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: const Text('Watch Together'),
            content: SizedBox(
              width: 340,
              child: users.isEmpty
                  ? const Text('No eligible household profiles or friends available to invite right now.')
                  : ListView.builder(
                      shrinkWrap: true,
                      itemCount: users.length,
                      itemBuilder: (c, i) {
                        final u = users[i];
                        final key = (u['target_key'] ?? u['id']).toString();
                        final label = (u['display_name'] ?? u['username'] ?? u['name'] ?? 'User').toString();
                        return CheckboxListTile(
                          value: selected.contains(key),
                          title: Text(label),
                          onChanged: (val) => setDialogState(() {
                            if (val == true) {
                              selected.add(key);
                            } else {
                              selected.remove(key);
                            }
                          }),
                        );
                      },
                    ),
            ),
            actions: [
              TextButton(onPressed: () => Navigator.pop(dCtx, false), child: const Text('Cancel')),
              ElevatedButton(
                onPressed: () => Navigator.pop(dCtx, true),
                child: const Text('Create Party'),
              ),
            ],
          ),
        ),
      );

      if (confirmed != true) return;
      final createResp = await dio.post('/api/watch_together/create', data: {
        'media_type': widget.mediaType,
        'item_id': widget.itemId,
        'invited_targets': selected.toList(),
      });
      final sessionId = createResp.data['session_id'];
      if (mounted) {
        Navigator.of(context).pop();
        _snack('Watch Together session started (ID: $sessionId)');
      }
    } catch (e) {
      if (mounted) {
        setState(() => _busy = false);
        _snack(formatApiError(e, fallback: 'Watch Together failed.'), isError: true);
      }
    }
  }

  Future<void> _refreshMetadata() async {
    final dio = ref.read(apiClientProvider);
    await _runMutation(
      () => dio.post('/api/media/refresh_metadata', data: {
        'media_type': widget.mediaType,
        'item_id': widget.itemId,
      }),
      'Metadata refreshed from TMDB',
    );
  }

  Future<void> _openFixMatchDialog() async {
    final dio = ref.read(apiClientProvider);
    final queryCtrl = TextEditingController(text: widget.title);
    final yearCtrl = TextEditingController(text: widget.year ?? '');
    List<Map<String, dynamic>> candidates = [];
    bool searching = false;
    String? searchError;

    Future<void> runSearch(StateSetter setModalState) async {
      final q = queryCtrl.text.trim();
      if (q.isEmpty) return;
      setModalState(() {
        searching = true;
        searchError = null;
      });
      try {
        final resp = await dio.get('/api/media/matches', queryParameters: {
          'media_type': widget.mediaType,
          'query': q,
          if (yearCtrl.text.trim().isNotEmpty) 'year': yearCtrl.text.trim(),
        });
        final raw = resp.data['candidates'] as List? ?? [];
        candidates = raw.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
      } catch (e) {
        searchError = formatApiError(e, fallback: 'TMDB search failed.');
      } finally {
        setModalState(() => searching = false);
      }
    }

    await showDialog(
      context: context,
      builder: (dCtx) => StatefulBuilder(
        builder: (context, setModalState) => AlertDialog(
          title: Text('Fix Match: ${widget.title}'),
          content: SizedBox(
            width: 380,
            height: 420,
            child: Column(
              children: [
                Row(
                  children: [
                    Expanded(
                      flex: 3,
                      child: TextField(
                        controller: queryCtrl,
                        decoration: const InputDecoration(labelText: 'Title'),
                        onSubmitted: (_) => runSearch(setModalState),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextField(
                        controller: yearCtrl,
                        decoration: const InputDecoration(labelText: 'Year'),
                        keyboardType: TextInputType.number,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    onPressed: searching ? null : () => runSearch(setModalState),
                    icon: const Icon(LucideIcons.search, size: 16),
                    label: Text(searching ? 'Searching...' : 'Search TMDB'),
                  ),
                ),
                if (searchError != null) ...[
                  const SizedBox(height: 8),
                  Text(searchError!, style: const TextStyle(color: AppColors.failure, fontSize: 12)),
                ],
                const SizedBox(height: 10),
                Expanded(
                  child: candidates.isEmpty
                      ? const Center(
                          child: Text(
                            'Run a search to pick the matching TMDB title.',
                            style: TextStyle(color: AppColors.textMuted, fontSize: 13),
                          ),
                        )
                      : ListView.separated(
                          itemCount: candidates.length,
                          separatorBuilder: (_, __) => const Divider(),
                          itemBuilder: (c, i) {
                            final cand = candidates[i];
                            return ListTile(
                              contentPadding: EdgeInsets.zero,
                              title: Text('${cand['title']} (${cand['year'] ?? ''})'),
                              subtitle: Text(
                                cand['overview']?.toString() ?? '',
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(fontSize: 12, color: AppColors.textMuted),
                              ),
                              trailing: const Icon(LucideIcons.checkCircle2, color: AppColors.accent),
                              onTap: () async {
                                Navigator.pop(dCtx);
                                await _runMutation(
                                  () => dio.post('/api/media/fix_match', data: {
                                    'media_type': widget.mediaType,
                                    'item_id': widget.itemId,
                                    'tmdb_id': cand['tmdb_id'],
                                  }),
                                  'Match updated to ${cand['title']}',
                                );
                              },
                            );
                          },
                        ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(dCtx), child: const Text('Close')),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(appSessionProvider);
    final isAdmin = session.isAdmin;
    final canWatchTogether = session.userInfo['watch_together_access'] != false;
    final canDownload = session.userInfo['downloads_access'] != false;

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 8),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 36,
              height: 4,
              margin: const EdgeInsets.only(bottom: 12),
              decoration: BoxDecoration(
                color: AppColors.surfaceHover,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
              child: Text(
                widget.title,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: AppColors.text),
              ),
            ),
            const Divider(),
            if (_busy)
              const Padding(
                padding: EdgeInsets.all(24),
                child: CircularProgressIndicator(color: AppColors.accent),
              )
            else ...[
              // 1. Mark Watched / Unwatched
              ListTile(
                leading: Icon(
                  widget.isWatched ? LucideIcons.eyeOff : LucideIcons.check,
                  color: AppColors.text,
                ),
                title: Text(widget.isWatched ? 'Mark as Unwatched' : 'Mark as Watched'),
                onTap: _toggleWatched,
              ),
              // 2. Remove Watch Progress
              if (widget.hasProgress || widget.isWatched)
                ListTile(
                  leading: const Icon(LucideIcons.rotateCcw, color: AppColors.text),
                  title: const Text('Remove Watch Progress'),
                  onTap: _removeWatchProgress,
                ),
              // 3. Watch Together
              if (canWatchTogether && (widget.mediaType == 'movie' || widget.mediaType == 'tv'))
                ListTile(
                  leading: const Icon(LucideIcons.users, color: AppColors.text),
                  title: const Text('Watch Together'),
                  onTap: _startWatchTogether,
                ),
              // 4. Download
              if (canDownload && (widget.mediaType == 'movie' || widget.mediaType == 'tv'))
                ListTile(
                  leading: const Icon(LucideIcons.download, color: AppColors.text),
                  title: const Text('Download'),
                  onTap: _downloadMedia,
                ),
              // 5. Add / Remove Watchlist
              ListTile(
                leading: Icon(
                  widget.inWatchlist ? LucideIcons.bookmarkMinus : LucideIcons.bookmarkPlus,
                  color: AppColors.text,
                ),
                title: Text(widget.inWatchlist ? 'Remove from Watchlist' : 'Add to Watchlist'),
                onTap: _toggleWatchlist,
              ),
              // 6. Refresh Metadata (Admin only)
              if (isAdmin && (widget.mediaType == 'movie' || widget.mediaType == 'tv'))
                ListTile(
                  leading: const Icon(LucideIcons.refreshCw, color: AppColors.text),
                  title: const Text('Refresh Metadata'),
                  onTap: _refreshMetadata,
                ),
              // 7. Fix Match (Admin only)
              if (isAdmin && (widget.mediaType == 'movie' || widget.mediaType == 'tv'))
                ListTile(
                  leading: const Icon(LucideIcons.wrench, color: AppColors.text),
                  title: const Text('Fix Match'),
                  onTap: _openFixMatchDialog,
                ),
            ],
          ],
        ),
      ),
    );
  }
}
