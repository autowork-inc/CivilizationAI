import { Ionicons } from '@expo/vector-icons';
import { Pressable, Text, View } from 'react-native';

type Props = {
  message: string;
  onRetry?: () => void;
  onDismiss?: () => void;
};

/** Red banner above the input: what went wrong, "Повторить" and a close button. */
export function ErrorBanner({ message, onRetry, onDismiss }: Props) {
  return (
    <View
      accessibilityRole="alert"
      accessibilityLiveRegion="assertive"
      className="mx-3 mb-2 flex-row items-center rounded-2xl border border-red-500/30 bg-red-950 px-3 py-2.5">
      <Ionicons name="alert-circle" size={20} color="#f87171" />
      <Text className="ml-2 mr-2 flex-1 text-sm leading-5 text-red-200">{message}</Text>
      {onRetry ? (
        <Pressable
          onPress={onRetry}
          hitSlop={8}
          accessibilityRole="button"
          className="mr-1 rounded-full bg-red-500/20 px-3 py-1.5 active:bg-red-500/30">
          <Text className="text-sm font-semibold text-red-100">Повторить</Text>
        </Pressable>
      ) : null}
      {onDismiss ? (
        <Pressable onPress={onDismiss} hitSlop={10} accessibilityRole="button" accessibilityLabel="Закрыть" className="p-1">
          <Ionicons name="close" size={18} color="#fca5a5" />
        </Pressable>
      ) : null}
    </View>
  );
}
