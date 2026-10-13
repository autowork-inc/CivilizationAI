import { Ionicons } from '@expo/vector-icons';
import { Pressable, Text, View } from 'react-native';

type Props = { onPress: () => void; title?: string };

/** "Войти через Google": the white button Google's brand guidelines expect, with the G mark. */
export function GoogleButton({ onPress, title = 'Войти через Google' }: Props) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      className="h-14 w-full flex-row items-center justify-center rounded-2xl bg-white active:scale-[0.98] active:bg-slate-100">
      <View className="mr-3">
        <Ionicons name="logo-google" size={20} color="#4285F4" />
      </View>
      <Text className="text-base font-semibold text-slate-800">{title}</Text>
    </Pressable>
  );
}
