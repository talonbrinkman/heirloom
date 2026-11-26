import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../theme.dart';
import 'media_action_sheet.dart';

class MediaCard extends StatelessWidget {
  final dynamic itemId;
  final String title;
  final String? subtitle;
  final String? imageUrl;
  final String mediaType;
  final double aspectRatio;
  final bool expandImage;
  final double? progress;
  final bool isWatched;
  final bool hasProgress;
  final bool inWatchlist;
  final String? year;
  final String? historyScope;
  final bool showActionMenu;
  final VoidCallback onTap;
  final VoidCallback? onMutated;

  const MediaCard({
    super.key,
    required this.itemId,
    required this.title,
    this.subtitle,
    this.imageUrl,
    required this.mediaType,
    this.aspectRatio = 2 / 3,
    this.expandImage = false,
    this.progress,
    this.isWatched = false,
    this.hasProgress = false,
    this.inWatchlist = false,
    this.year,
    this.historyScope,
    this.showActionMenu = true,
    required this.onTap,
    this.onMutated,
  });

  @override
  Widget build(BuildContext context) {
    final numericId = int.tryParse(itemId?.toString() ?? '');
    final canOpenSheet = showActionMenu && numericId != null && (mediaType == 'movie' || mediaType == 'tv');

    final imageBox = ClipRRect(
      borderRadius: BorderRadius.circular(AppSpacing.radius),
      child: Container(
        width: double.infinity,
        color: AppColors.surface,
        child: Stack(
          fit: StackFit.expand,
          children: [
            if (imageUrl != null)
              CachedNetworkImage(
                imageUrl: imageUrl!,
                fit: BoxFit.cover,
                errorWidget: (_, __, ___) => const Center(
                  child: Icon(LucideIcons.imageOff, color: AppColors.textMuted, size: 32),
                ),
              )
            else
              Center(
                child: Icon(
                  mediaType == 'album' || mediaType == 'music' || mediaType == 'track'
                      ? LucideIcons.music
                      : (mediaType == 'photo' ? LucideIcons.folder : LucideIcons.film),
                  color: AppColors.textMuted,
                  size: 32,
                ),
              ),

            if (isWatched)
              Positioned(
                top: 6,
                left: 6,
                child: Container(
                  padding: const EdgeInsets.all(4),
                  decoration: const BoxDecoration(
                    color: AppColors.success,
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(LucideIcons.check, size: 12, color: AppColors.text),
                ),
              ),

            if (canOpenSheet)
              Positioned(
                top: 4,
                right: 4,
                child: Material(
                  color: AppColors.glass,
                  shape: const CircleBorder(),
                  child: InkWell(
                    customBorder: const CircleBorder(),
                    onTap: () => showMediaActionSheet(
                      context: context,
                      mediaType: mediaType,
                      itemId: numericId,
                      title: title,
                      year: year,
                      isWatched: isWatched,
                      hasProgress: hasProgress || (progress != null && progress! > 0),
                      inWatchlist: inWatchlist,
                      historyScope: historyScope,
                      onMutated: onMutated,
                    ),
                    child: const Padding(
                      padding: EdgeInsets.all(5),
                      child: Icon(LucideIcons.moreVertical, size: 15, color: AppColors.text),
                    ),
                  ),
                ),
              ),

            if (!isWatched && progress != null && progress! > 0)
              Positioned(
                left: 0,
                right: 0,
                bottom: 0,
                child: Container(
                  height: 4,
                  color: Colors.white24,
                  child: FractionallySizedBox(
                    alignment: Alignment.centerLeft,
                    widthFactor: (progress! / 100.0).clamp(0.0, 1.0),
                    child: Container(color: AppColors.accent),
                  ),
                ),
              ),
          ],
        ),
      ),
    );

    return InkWell(
      borderRadius: BorderRadius.circular(AppSpacing.radius),
      onTap: onTap,
      onLongPress: canOpenSheet
          ? () => showMediaActionSheet(
                context: context,
                mediaType: mediaType,
                itemId: numericId,
                title: title,
                year: year,
                isWatched: isWatched,
                hasProgress: hasProgress || (progress != null && progress! > 0),
                inWatchlist: inWatchlist,
                historyScope: historyScope,
                onMutated: onMutated,
              )
          : null,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (expandImage)
            Expanded(child: imageBox)
          else
            AspectRatio(aspectRatio: aspectRatio, child: imageBox),
          const SizedBox(height: AppSpacing.gap),
          Text(
            title,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: AppColors.text,
              fontWeight: FontWeight.w600,
              fontSize: 13,
            ),
          ),
          if (subtitle != null && subtitle!.isNotEmpty)
            Text(
              subtitle!,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: AppColors.textMuted, fontSize: 11),
            ),
        ],
      ),
    );
  }
}
