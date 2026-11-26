import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import 'audio_player_controller.dart';

class MiniPlayerBar extends ConsumerWidget {
  const MiniPlayerBar({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final audioState = ref.watch(audioPlayerProvider);
    final track = audioState.currentTrack;
    if (track == null) return const SizedBox.shrink();

    final serverUrl = ref.watch(serverUrlProvider);
    final coverUrl = buildMetadataImageUrl(serverUrl, track.coverUrl);
    final totalMs = audioState.duration.inMilliseconds > 0
        ? audioState.duration.inMilliseconds
        : (track.durationSeconds * 1000).round();
    final progressRatio = totalMs > 0
        ? (audioState.position.inMilliseconds / totalMs).clamp(0.0, 1.0)
        : 0.0;

    return GestureDetector(
      onTap: () => _openNowPlayingModal(context),
      child: Container(
        color: AppColors.surface,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              child: Row(
                children: [
                  ClipRRect(
                    borderRadius: BorderRadius.circular(AppSpacing.radius),
                    child: Container(
                      width: 42,
                      height: 42,
                      color: AppColors.bg,
                      child: coverUrl != null
                          ? CachedNetworkImage(
                              imageUrl: coverUrl,
                              fit: BoxFit.cover,
                              errorWidget: (_, __, ___) => const Icon(LucideIcons.music, color: AppColors.textMuted),
                            )
                          : const Icon(LucideIcons.music, color: AppColors.textMuted),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          track.title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(color: AppColors.text, fontWeight: FontWeight.w600, fontSize: 13),
                        ),
                        Text(
                          track.artistName,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(color: AppColors.textMuted, fontSize: 11),
                        ),
                      ],
                    ),
                  ),
                  IconButton(
                    icon: audioState.isLoading
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.accent),
                          )
                        : Icon(
                            audioState.isPlaying ? LucideIcons.pause : LucideIcons.play,
                            color: AppColors.text,
                            size: 20,
                          ),
                    onPressed: () => ref.read(audioPlayerProvider.notifier).togglePlayPause(),
                  ),
                  IconButton(
                    icon: const Icon(LucideIcons.skipForward, color: AppColors.text, size: 20),
                    onPressed: () => ref.read(audioPlayerProvider.notifier).next(),
                  ),
                  IconButton(
                    icon: const Icon(LucideIcons.x, color: AppColors.textMuted, size: 18),
                    onPressed: () => ref.read(audioPlayerProvider.notifier).stop(),
                  ),
                ],
              ),
            ),
            LinearProgressIndicator(
              value: progressRatio,
              minHeight: 2,
              backgroundColor: AppColors.surfaceHover,
              color: AppColors.accent,
            ),
          ],
        ),
      ),
    );
  }

  void _openNowPlayingModal(BuildContext context) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppColors.bg,
      builder: (_) => const _NowPlayingSheet(),
    );
  }
}

class _NowPlayingSheet extends ConsumerWidget {
  const _NowPlayingSheet();

  String _fmt(Duration d) {
    final m = d.inMinutes;
    final s = d.inSeconds % 60;
    return '$m:${s.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(audioPlayerProvider);
    final track = state.currentTrack;
    if (track == null) return const SizedBox.shrink();
    final serverUrl = ref.watch(serverUrlProvider);
    final coverUrl = buildMetadataImageUrl(serverUrl, track.coverUrl);

    final totalDur = state.duration.inSeconds > 0
        ? state.duration
        : Duration(seconds: track.durationSeconds.round());
    final maxMs = totalDur.inMilliseconds > 0 ? totalDur.inMilliseconds.toDouble() : 1.0;
    final curMs = state.position.inMilliseconds.toDouble().clamp(0.0, maxMs);

    return SafeArea(
      child: DraggableScrollableSheet(
        initialChildSize: 0.9,
        maxChildSize: 0.96,
        minChildSize: 0.5,
        expand: false,
        builder: (ctx, scrollCtrl) => SingleChildScrollView(
          controller: scrollCtrl,
          padding: const EdgeInsets.all(20),
          child: Column(
            children: [
              Container(
                width: 38,
                height: 4,
                margin: const EdgeInsets.only(bottom: 18),
                decoration: BoxDecoration(
                  color: AppColors.surfaceHover,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              ClipRRect(
                borderRadius: BorderRadius.circular(AppSpacing.radius),
                child: Container(
                  width: 240,
                  height: 240,
                  color: AppColors.surface,
                  child: coverUrl != null
                      ? CachedNetworkImage(imageUrl: coverUrl, fit: BoxFit.cover)
                      : const Icon(LucideIcons.music, size: 72, color: AppColors.textMuted),
                ),
              ),
              const SizedBox(height: 22),
              Text(
                track.title,
                textAlign: TextAlign.center,
                style: const TextStyle(color: AppColors.text, fontSize: 22, fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 4),
              Text(
                '${track.artistName}${track.albumTitle != null ? ' • ${track.albumTitle}' : ''}',
                textAlign: TextAlign.center,
                style: const TextStyle(color: AppColors.textMuted, fontSize: 14),
              ),
              const SizedBox(height: 18),
              Slider(
                value: curMs,
                max: maxMs,
                activeColor: AppColors.accent,
                inactiveColor: AppColors.surfaceHover,
                onChanged: (val) =>
                    ref.read(audioPlayerProvider.notifier).seek(Duration(milliseconds: val.round())),
              ),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(_fmt(state.position), style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
                    Text(_fmt(totalDur), style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
                  ],
                ),
              ),
              const SizedBox(height: 14),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  IconButton(
                    icon: Icon(
                      LucideIcons.shuffle,
                      color: state.shuffleEnabled ? AppColors.accent : AppColors.textMuted,
                    ),
                    onPressed: () => ref.read(audioPlayerProvider.notifier).toggleShuffle(),
                  ),
                  IconButton(
                    iconSize: 32,
                    icon: const Icon(LucideIcons.skipBack, color: AppColors.text),
                    onPressed: () => ref.read(audioPlayerProvider.notifier).previous(),
                  ),
                  FloatingActionButton(
                    backgroundColor: AppColors.accent,
                    foregroundColor: AppColors.text,
                    onPressed: () => ref.read(audioPlayerProvider.notifier).togglePlayPause(),
                    child: Icon(state.isPlaying ? LucideIcons.pause : LucideIcons.play),
                  ),
                  IconButton(
                    iconSize: 32,
                    icon: const Icon(LucideIcons.skipForward, color: AppColors.text),
                    onPressed: () => ref.read(audioPlayerProvider.notifier).next(),
                  ),
                  IconButton(
                    icon: Icon(
                      state.loopMode == LoopMode.one ? LucideIcons.repeat1 : LucideIcons.repeat,
                      color: state.loopMode != LoopMode.off ? AppColors.accent : AppColors.textMuted,
                    ),
                    onPressed: () => ref.read(audioPlayerProvider.notifier).cycleLoopMode(),
                  ),
                ],
              ),
              if (state.queue.length > 1) ...[
                const SizedBox(height: 24),
                const Divider(),
                const Align(
                  alignment: Alignment.centerLeft,
                  child: Padding(
                    padding: EdgeInsets.symmetric(vertical: 8),
                    child: Text('Up Next', style: TextStyle(fontSize: 15, fontWeight: FontWeight.bold)),
                  ),
                ),
                ...state.queue.asMap().entries.map((entry) {
                  final idx = entry.key;
                  final t = entry.value;
                  final isCurrent = idx == state.currentIndex;
                  return ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    leading: Icon(
                      isCurrent ? LucideIcons.volume2 : LucideIcons.music,
                      size: 16,
                      color: isCurrent ? AppColors.accent : AppColors.textMuted,
                    ),
                    title: Text(
                      t.title,
                      style: TextStyle(
                        color: isCurrent ? AppColors.accent : AppColors.text,
                        fontWeight: isCurrent ? FontWeight.bold : FontWeight.normal,
                      ),
                    ),
                    subtitle: Text(t.artistName, style: const TextStyle(color: AppColors.textMuted, fontSize: 11)),
                    onTap: () => ref.read(audioPlayerProvider.notifier).playQueue(state.queue, startIndex: idx),
                  );
                }),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
