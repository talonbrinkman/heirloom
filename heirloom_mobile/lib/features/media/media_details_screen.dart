import 'package:cached_network_image/cached_network_image.dart';
import 'package:dio/dio.dart' as dio_pkg;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:url_launcher/url_launcher.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../core/widgets/media_action_sheet.dart';

final mediaDetailsProvider = FutureProvider.family<Map<String, dynamic>, String>((ref, param) async {
  final dio = ref.watch(apiClientProvider);
  final colonIdx = param.indexOf(':');
  final mediaType = colonIdx != -1 ? param.substring(0, colonIdx) : 'movie';
  final id = colonIdx != -1 ? param.substring(colonIdx + 1) : param;

  if (mediaType == 'photo') {
    final response = await dio.get('/api/photo/$id');
    return Map<String, dynamic>.from(response.data as Map);
  }

  final endpoint = mediaType == 'movie' ? '/api/movie/$id' : '/api/series/$id';
  try {
    final response = await dio.get(
      endpoint,
      options: dio_pkg.Options(headers: {'Accept': 'application/json'}),
    );
    return Map<String, dynamic>.from(response.data as Map);
  } on dio_pkg.DioException catch (e) {
    if (e.response?.statusCode == 404) {
      final fallbackEndpoint = mediaType == 'movie' ? '/api/series/$id' : '/api/movie/$id';
      try {
        final fallbackResp = await dio.get(
          fallbackEndpoint,
          options: dio_pkg.Options(headers: {'Accept': 'application/json'}),
        );
        return Map<String, dynamic>.from(fallbackResp.data as Map);
      } catch (_) {}
    }
    rethrow;
  }
});

class MediaDetailsScreen extends ConsumerStatefulWidget {
  final String mediaType;
  final String id;

  const MediaDetailsScreen({
    super.key,
    required this.mediaType,
    required this.id,
  });

  @override
  ConsumerState<MediaDetailsScreen> createState() => _MediaDetailsScreenState();
}

class _MediaDetailsScreenState extends ConsumerState<MediaDetailsScreen> {
  int? _selectedSeason;
  bool? _inWatchlist;

  void _refresh() {
    ref.invalidate(mediaDetailsProvider('${widget.mediaType}:${widget.id}'));
  }

  @override
  Widget build(BuildContext context) {
    final param = '${widget.mediaType}:${widget.id}';
    final detailsAsync = ref.watch(mediaDetailsProvider(param));
    final session = ref.watch(appSessionProvider);
    final serverUrl = session.serverUrl ?? '';
    final canDownload = session.userInfo['downloads_access'] != false;

    return Scaffold(
      extendBodyBehindAppBar: true,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft, color: AppColors.text, shadows: [Shadow(color: Colors.black, blurRadius: 6)]),
          onPressed: () => context.pop(),
        ),
        actions: [
          detailsAsync.maybeWhen(
            data: (data) {
              final item = data['item'] ?? data['movie'] ?? data['tv_show'] ?? data;
              final numericId = int.tryParse('${item['id']}');
              if (numericId == null) return const SizedBox.shrink();
              return IconButton(
                icon: const Icon(LucideIcons.moreVertical, color: AppColors.text, shadows: [Shadow(color: Colors.black, blurRadius: 6)]),
                onPressed: () => showMediaActionSheet(
                  context: context,
                  mediaType: widget.mediaType,
                  itemId: numericId,
                  title: (item['title'] ?? 'Title').toString(),
                  year: item['year']?.toString(),
                  isWatched: item['is_watched'] == true,
                  hasProgress: item['has_progress'] == true,
                  inWatchlist: _inWatchlist ?? (item['in_watchlist'] == true),
                  historyScope: widget.mediaType == 'tv' ? 'series' : null,
                  onMutated: _refresh,
                ),
              );
            },
            orElse: () => const SizedBox.shrink(),
          ),
        ],
      ),
      body: detailsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator(color: AppColors.accent)),
        error: (err, stack) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24.0),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(LucideIcons.film, size: 48, color: AppColors.textMuted),
                const SizedBox(height: 16),
                const Text('Media Item Not Found', style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
                const SizedBox(height: 8),
                const Text(
                  'This movie or TV show could not be found on the server.',
                  style: TextStyle(color: AppColors.textMuted, fontSize: 13),
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 20),
                ElevatedButton.icon(
                  onPressed: () => context.pop(),
                  icon: const Icon(LucideIcons.arrowLeft, size: 16),
                  label: const Text('Go Back'),
                ),
              ],
            ),
          ),
        ),
        data: (data) {
          final item = data['item'] ?? data['movie'] ?? data['tv_show'] ?? data;
          final backdrop = item['backdrop_filename']?.toString();
          final poster = item['poster_filename']?.toString();
          final title = item['title']?.toString() ?? 'Unknown Title';
          final plot = item['plot']?.toString() ?? 'No plot available.';
          final year = item['year']?.toString() ?? '';
          final rating = item['rating']?.toString() ?? '';
          final runtime = item['runtime']?.toString() ?? '';
          final contentRating = item['content_rating']?.toString() ?? '';
          final genresRaw = item['genres']?.toString() ?? '';
          final genres = genresRaw.split(',').map((g) => g.trim()).where((g) => g.isNotEmpty).toList();
          final cast = (item['cast'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
          final isWatched = item['is_watched'] == true;
          final hasMovieProgress = item['has_progress'] == true;
          final movieProgress = (item['progress'] as num?)?.toDouble() ?? 0.0;

          final rawWatchlist = item['in_watchlist'] as bool? ?? false;
          _inWatchlist ??= rawWatchlist;

          final backdropUrl = buildMetadataImageUrl(serverUrl, backdrop ?? poster);

          // Parse seasons & episodes if TV Show
          final seasonsMap = <int, List<Map<String, dynamic>>>{};
          if (widget.mediaType == 'tv' && item['seasons'] is Map) {
            (item['seasons'] as Map).forEach((k, v) {
              final sNum = int.tryParse(k.toString()) ?? 1;
              if (v is List) {
                seasonsMap[sNum] = v.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
              }
            });
          }

          final sortedSeasonNumbers = seasonsMap.keys.toList()..sort();
          if (_selectedSeason == null && sortedSeasonNumbers.isNotEmpty) {
            _selectedSeason = sortedSeasonNumbers.first;
          }

          dynamic playTargetId = item['id'] ?? widget.id;
          bool targetHasProgress = hasMovieProgress;
          String playButtonLabel = hasMovieProgress ? 'Resume' : 'Play';

          if (widget.mediaType == 'tv' && seasonsMap.isNotEmpty) {
            final activeEpisodes = seasonsMap[_selectedSeason] ?? const [];
            if (activeEpisodes.isNotEmpty) {
              final uncompleted = activeEpisodes.firstWhere(
                (ep) => (ep['progress'] as num? ?? 0) < 90,
                orElse: () => activeEpisodes.first,
              );
              playTargetId = uncompleted['id'];
              final epNum = uncompleted['episode'] ?? '';
              final epProg = (uncompleted['progress'] as num?)?.toDouble() ?? 0.0;
              targetHasProgress = epProg > 0 && epProg < 90;
              playButtonLabel = targetHasProgress ? 'Resume S$_selectedSeason E$epNum' : 'Play S$_selectedSeason E$epNum';
            }
          }

          return SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  height: 300,
                  width: double.infinity,
                  child: Stack(
                    fit: StackFit.expand,
                    children: [
                      if (backdropUrl != null)
                        CachedNetworkImage(
                          imageUrl: backdropUrl,
                          fit: BoxFit.cover,
                          errorWidget: (_, __, ___) => Container(color: AppColors.surface),
                        )
                      else
                        Container(color: AppColors.surface),
                      Container(
                        decoration: BoxDecoration(
                          gradient: LinearGradient(
                            begin: Alignment.topCenter,
                            end: Alignment.bottomCenter,
                            colors: [
                              Colors.transparent,
                              AppColors.bg.withOpacity(0.75),
                              AppColors.bg,
                            ],
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16.0),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Expanded(
                            child: Text(title, style: const TextStyle(fontSize: 28, fontWeight: FontWeight.bold)),
                          ),
                          if (isWatched)
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                              decoration: BoxDecoration(
                                color: AppColors.success.withOpacity(0.2),
                                borderRadius: BorderRadius.circular(AppSpacing.radius),
                              ),
                              child: const Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Icon(LucideIcons.check, size: 13, color: AppColors.success),
                                  SizedBox(width: 4),
                                  Text('Watched', style: TextStyle(color: AppColors.success, fontSize: 12, fontWeight: FontWeight.bold)),
                                ],
                              ),
                            ),
                        ],
                      ),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        runSpacing: 6,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          if (year.isNotEmpty) _buildTag(year),
                          if (contentRating.isNotEmpty) _buildTag(contentRating),
                          if (runtime.isNotEmpty) _buildTag('$runtime min'),
                          if (rating.isNotEmpty)
                            Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                const Icon(LucideIcons.star, color: AppColors.ratingGold, size: 15),
                                const SizedBox(width: 4),
                                Text(rating, style: const TextStyle(fontWeight: FontWeight.bold)),
                              ],
                            ),
                        ],
                      ),
                      if (widget.mediaType == 'movie' && hasMovieProgress && movieProgress > 0) ...[
                        const SizedBox(height: 10),
                        ClipRRect(
                          borderRadius: BorderRadius.circular(2),
                          child: LinearProgressIndicator(
                            value: (movieProgress / 100.0).clamp(0.0, 1.0),
                            color: AppColors.accent,
                            backgroundColor: AppColors.surfaceHover,
                            minHeight: 4,
                          ),
                        ),
                      ],
                      const SizedBox(height: 18),
                      Row(
                        children: [
                          Expanded(
                            child: ElevatedButton.icon(
                              onPressed: () => context.push('/play/${widget.mediaType}/$playTargetId'),
                              icon: const Icon(LucideIcons.play),
                              label: Text(playButtonLabel, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                            ),
                          ),
                          if (targetHasProgress) ...[
                            const SizedBox(width: 8),
                            OutlinedButton.icon(
                              onPressed: () => context.push('/play/${widget.mediaType}/$playTargetId?restart=1'),
                              icon: const Icon(LucideIcons.rotateCcw, size: 16),
                              label: const Text('Restart'),
                            ),
                          ],
                        ],
                      ),
                      const SizedBox(height: 14),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                        children: [
                          _buildActionButton(
                            _inWatchlist == true ? LucideIcons.check : LucideIcons.plus,
                            _inWatchlist == true ? 'In Watchlist' : 'Watchlist',
                            () => _toggleWatchlist(item['id']),
                            isActive: _inWatchlist == true,
                          ),
                          _buildActionButton(
                            isWatched ? LucideIcons.eyeOff : LucideIcons.checkCircle2,
                            isWatched ? 'Unwatch' : 'Mark Watched',
                            () => _toggleWatched(item['id'], isWatched),
                            isActive: isWatched,
                          ),
                          if (canDownload)
                            _buildActionButton(LucideIcons.download, 'Download', () async {
                              final url = '$serverUrl/download/${widget.mediaType}/${item['id']}?token=${session.authToken}';
                              await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
                            }),
                          _buildActionButton(LucideIcons.moreHorizontal, 'More', () {
                            final numericId = int.tryParse('${item['id']}');
                            if (numericId == null) return;
                            showMediaActionSheet(
                              context: context,
                              mediaType: widget.mediaType,
                              itemId: numericId,
                              title: title,
                              year: year,
                              isWatched: isWatched,
                              hasProgress: hasMovieProgress,
                              inWatchlist: _inWatchlist ?? false,
                              historyScope: widget.mediaType == 'tv' ? 'series' : null,
                              onMutated: _refresh,
                            );
                          }),
                        ],
                      ),
                      if (genres.isNotEmpty) ...[
                        const SizedBox(height: 14),
                        Wrap(
                          spacing: 6,
                          runSpacing: 6,
                          children: genres
                              .map((g) => Chip(
                                    label: Text(g, style: const TextStyle(fontSize: 11)),
                                    padding: EdgeInsets.zero,
                                    visualDensity: VisualDensity.compact,
                                  ))
                              .toList(),
                        ),
                      ],
                      const SizedBox(height: 16),
                      Text(
                        plot,
                        style: const TextStyle(fontSize: 15, color: AppColors.textMuted, height: 1.5),
                      ),
                      if (cast.isNotEmpty) ...[
                        const SizedBox(height: 24),
                        const Text('Cast', style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
                        const SizedBox(height: 10),
                        SizedBox(
                          height: 120,
                          child: ListView.builder(
                            scrollDirection: Axis.horizontal,
                            itemCount: cast.length.clamp(0, 15),
                            itemBuilder: (ctx, i) {
                              final c = cast[i];
                              final photoUrl = buildMetadataImageUrl(
                                serverUrl,
                                (c['profile_filename'] ?? c['profile_path'])?.toString(),
                              );
                              return Container(
                                width: 84,
                                margin: const EdgeInsets.only(right: 10),
                                child: Column(
                                  children: [
                                    CircleAvatar(
                                      radius: 30,
                                      backgroundColor: AppColors.surface,
                                      backgroundImage: photoUrl != null ? CachedNetworkImageProvider(photoUrl) : null,
                                      child: photoUrl == null ? const Icon(LucideIcons.user, color: AppColors.textMuted) : null,
                                    ),
                                    const SizedBox(height: 6),
                                    Text(
                                      c['name']?.toString() ?? '',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w600),
                                      textAlign: TextAlign.center,
                                    ),
                                    if (c['character'] != null)
                                      Text(
                                        c['character'].toString(),
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: const TextStyle(fontSize: 10, color: AppColors.textMuted),
                                        textAlign: TextAlign.center,
                                      ),
                                  ],
                                ),
                              );
                            },
                          ),
                        ),
                      ],
                      const SizedBox(height: 24),
                      if (widget.mediaType == 'tv' && seasonsMap.isNotEmpty) ...[
                        const Divider(),
                        const SizedBox(height: 10),
                        const Text('Episodes', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
                        const SizedBox(height: 10),
                        SingleChildScrollView(
                          scrollDirection: Axis.horizontal,
                          child: Row(
                            children: sortedSeasonNumbers.map((sNum) {
                              final isSelected = sNum == _selectedSeason;
                              return Padding(
                                padding: const EdgeInsets.only(right: 8.0),
                                child: ChoiceChip(
                                  label: Text('Season $sNum'),
                                  selected: isSelected,
                                  selectedColor: AppColors.accent,
                                  backgroundColor: AppColors.surface,
                                  labelStyle: TextStyle(
                                    color: isSelected ? AppColors.text : AppColors.textMuted,
                                    fontWeight: isSelected ? FontWeight.bold : FontWeight.normal,
                                  ),
                                  onSelected: (val) {
                                    if (val) setState(() => _selectedSeason = sNum);
                                  },
                                ),
                              );
                            }).toList(),
                          ),
                        ),
                        const SizedBox(height: 14),
                        if (_selectedSeason != null && seasonsMap[_selectedSeason] != null)
                          ...seasonsMap[_selectedSeason]!.map((ep) {
                            final epNum = ep['episode'] ?? '';
                            final epId = ep['id'];
                            final epTitle = ep['title']?.toString() ?? 'Episode $epNum';
                            final epOverview = ep['overview']?.toString() ?? '';
                            final epStill = buildMetadataImageUrl(serverUrl, ep['still_filename']?.toString());
                            final progress = (ep['progress'] as num?)?.toDouble() ?? 0.0;
                            final epWatched = ep['is_watched'] == true || progress >= 90.0;
                            final epHasProg = progress > 0 && !epWatched;

                            return Card(
                              color: AppColors.surface,
                              margin: const EdgeInsets.only(bottom: 10),
                              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSpacing.radius)),
                              child: InkWell(
                                borderRadius: BorderRadius.circular(AppSpacing.radius),
                                onTap: () => context.push('/play/tv/$epId'),
                                child: Column(
                                  children: [
                                    Padding(
                                      padding: const EdgeInsets.all(10),
                                      child: Row(
                                        children: [
                                          ClipRRect(
                                            borderRadius: BorderRadius.circular(4),
                                            child: Container(
                                              width: 96,
                                              height: 56,
                                              color: AppColors.bg,
                                              child: Stack(
                                                fit: StackFit.expand,
                                                children: [
                                                  if (epStill != null)
                                                    CachedNetworkImage(imageUrl: epStill, fit: BoxFit.cover)
                                                  else
                                                    Center(child: Text('E$epNum', style: const TextStyle(fontWeight: FontWeight.bold))),
                                                  if (epWatched)
                                                    const Positioned(
                                                      top: 4,
                                                      left: 4,
                                                      child: CircleAvatar(
                                                        radius: 9,
                                                        backgroundColor: AppColors.success,
                                                        child: Icon(LucideIcons.check, size: 11, color: AppColors.text),
                                                      ),
                                                    ),
                                                ],
                                              ),
                                            ),
                                          ),
                                          const SizedBox(width: 12),
                                          Expanded(
                                            child: Column(
                                              crossAxisAlignment: CrossAxisAlignment.start,
                                              children: [
                                                Text(
                                                  'E$epNum • $epTitle',
                                                  maxLines: 1,
                                                  overflow: TextOverflow.ellipsis,
                                                  style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 14),
                                                ),
                                                if (epOverview.isNotEmpty) ...[
                                                  const SizedBox(height: 4),
                                                  Text(
                                                    epOverview,
                                                    maxLines: 2,
                                                    overflow: TextOverflow.ellipsis,
                                                    style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                                                  ),
                                                ],
                                              ],
                                            ),
                                          ),
                                          IconButton(
                                            icon: const Icon(LucideIcons.moreVertical, size: 18, color: AppColors.textMuted),
                                            onPressed: () {
                                              final numericEpId = int.tryParse('$epId');
                                              if (numericEpId == null) return;
                                              showMediaActionSheet(
                                                context: context,
                                                mediaType: 'tv',
                                                itemId: numericEpId,
                                                title: '$title S$_selectedSeason E$epNum',
                                                isWatched: epWatched,
                                                hasProgress: epHasProg,
                                                inWatchlist: _inWatchlist ?? false,
                                                historyScope: 'episode',
                                                onMutated: _refresh,
                                              );
                                            },
                                          ),
                                        ],
                                      ),
                                    ),
                                    if (epHasProg)
                                      ClipRRect(
                                        borderRadius: const BorderRadius.vertical(bottom: Radius.circular(6)),
                                        child: LinearProgressIndicator(
                                          value: (progress / 100.0).clamp(0.0, 1.0),
                                          color: AppColors.accent,
                                          backgroundColor: AppColors.surfaceHover,
                                          minHeight: 3,
                                        ),
                                      ),
                                  ],
                                ),
                              ),
                            );
                          }),
                      ],
                      const SizedBox(height: 48),
                    ],
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }

  Future<void> _toggleWatchlist(dynamic itemId) async {
    final dio = ref.read(apiClientProvider);
    final prev = _inWatchlist ?? false;
    setState(() => _inWatchlist = !prev);
    try {
      await dio.post(
        '/watchlist/toggle',
        data: {
          'media_type': widget.mediaType,
          'item_id': int.tryParse(itemId.toString()) ?? int.tryParse(widget.id) ?? 0,
        },
      );
    } catch (_) {
      if (mounted) setState(() => _inWatchlist = prev);
    }
  }

  Future<void> _toggleWatched(dynamic itemId, bool currentlyWatched) async {
    final dio = ref.read(apiClientProvider);
    final endpoint = currentlyWatched
        ? '/history/mark_unwatched/${widget.mediaType}/$itemId'
        : '/history/mark_watched/${widget.mediaType}/$itemId';
    await dio.post(
      endpoint,
      queryParameters: widget.mediaType == 'tv' ? {'scope': 'series'} : null,
    );
    _refresh();
  }

  Widget _buildTag(String text) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(AppSpacing.radius),
      ),
      child: Text(text, style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
    );
  }

  Widget _buildActionButton(IconData icon, String label, VoidCallback onTap, {bool isActive = false}) {
    return GestureDetector(
      onTap: onTap,
      child: Column(
        children: [
          Container(
            padding: const EdgeInsets.all(11),
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: isActive ? AppColors.accentTransparent : AppColors.surface,
            ),
            child: Icon(icon, color: isActive ? AppColors.accent : AppColors.text, size: 20),
          ),
          const SizedBox(height: 6),
          Text(label, style: const TextStyle(color: AppColors.textMuted, fontSize: 11)),
        ],
      ),
    );
  }
}
