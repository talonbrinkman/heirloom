import 'package:flutter/material.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/theme.dart';

void showHeirloomPhotoViewer(BuildContext context, Map<String, dynamic> photo, String serverUrl) {
  final posterFilename = photo['poster_filename'] ?? '';
  final photoId = photo['id'];
  final title = photo['title'] ?? 'Photo';
  final date = photo['date'] ?? photo['date_display'] ?? '';

  final imageUrl = posterFilename.isNotEmpty
      ? '$serverUrl/metadata/$posterFilename'
      : (photoId != null ? '$serverUrl/photo_file/$photoId' : null);

  showDialog(
    context: context,
    useSafeArea: false,
    barrierColor: Colors.black,
    builder: (ctx) => Scaffold(
      backgroundColor: Colors.black,
      body: Stack(
        children: [
          // Pinch-to-zoom interactive photo viewer
          Center(
            child: InteractiveViewer(
              minScale: 0.8,
              maxScale: 4.0,
              child: imageUrl != null
                  ? CachedNetworkImage(
                      imageUrl: imageUrl,
                      fit: BoxFit.contain,
                      placeholder: (context, url) => const Center(
                        child: CircularProgressIndicator(color: AppTheme.primaryRed),
                      ),
                      errorWidget: (context, url, error) => const Center(
                        child: Icon(LucideIcons.imageOff, size: 64, color: AppTheme.textTertiary),
                      ),
                    )
                  : const Center(child: Icon(LucideIcons.imageOff, size: 64, color: AppTheme.textTertiary)),
            ),
          ),

          // Top action bar with back button & title
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8.0, vertical: 8.0),
              child: Row(
                children: [
                  IconButton(
                    icon: const Icon(LucideIcons.arrowLeft, color: Colors.white),
                    onPressed: () => Navigator.of(ctx).pop(),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          title,
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 16,
                            fontWeight: FontWeight.bold,
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                        if (date.isNotEmpty)
                          Text(
                            date,
                            style: const TextStyle(
                              color: AppTheme.textSecondary,
                              fontSize: 12,
                            ),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    ),
  );
}

